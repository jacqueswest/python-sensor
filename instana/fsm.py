from __future__ import absolute_import

import os
import re
import socket
import subprocess
import sys
import threading as t

import fysom as f
from pkg_resources import get_distribution

from .agent_const import AGENT_HEADER, AGENT_DISCOVERY_URL, AGENT_DATA_URL, AGENT_DEFAULT_HOST, AGENT_DEFAULT_PORT
from .log import logger


class Discovery(object):
    pid = 0
    name = None
    args = None
    fd = -1
    inode = ""

    def __init__(self, **kwds):
        self.__dict__.update(kwds)

    def to_dict(self):
        kvs = dict()
        kvs['pid'] = self.pid
        kvs['name'] = self.name
        kvs['args'] = self.args
        kvs['fd'] = self.fd
        kvs['inode'] = self.inode
        return kvs


class Fsm(object):
    RETRY_PERIOD = 30

    agent = None
    fsm = None
    timer = None

    warnedPeriodic = False

    def __init__(self, agent):
        logger.info("Stan is on the scene.  Starting Instana instrumentation version: %s" %
                    get_distribution('instana').version)
        logger.debug("initializing fsm")

        self.agent = agent
        self.fsm = f.Fysom({
            "events": [
                ("lookup",   "*",            "found"),
                ("announce", "found",        "announced"),
                ("ready",    "announced",    "good2go")],
            "callbacks": {
                "onlookup":       self.lookup_agent_host,
                "onannounce":     self.announce_sensor,
                "onready":        self.start_metric_reporting,
                "onchangestate":  self.printstatechange}})

        timer = t.Timer(2, self.fsm.lookup)
        timer.daemon = True
        timer.name = "Startup"
        timer.start()

    def printstatechange(self, e):
        logger.debug('========= (%i#%s) FSM event: %s, src: %s, dst: %s ==========' %
                     (os.getpid(), t.current_thread().name, e.event, e.src, e.dst))

    def reset(self):
        self.fsm.lookup()

    def start_metric_reporting(self, e):
        self.agent.sensor.meter.run()

    def lookup_agent_host(self, e):
        host, port = self.__get_agent_host_port()

        h = self.check_host(host, port)
        if h == AGENT_HEADER:
            self.agent.host = host
            self.agent.port = port
            self.fsm.announce()
            return True
        elif os.path.exists("/proc/"):
            host = self.get_default_gateway()
            if host:
                h = self.check_host(host, port)
                if h == AGENT_HEADER:
                    self.agent.host = host
                    self.agent.port = port
                    self.fsm.announce()
                    return True

        if (self.warnedPeriodic is False):
            logger.warn("Instana Host Agent couldn't be found. Will retry periodically...")
            self.warnedPeriodic = True

        self.schedule_retry(self.lookup_agent_host, e, "agent_lookup")
        return False

    def get_default_gateway(self):
        logger.debug("checking default gateway")

        try:
            proc = subprocess.Popen(
                "/sbin/ip route | awk '/default/' | cut -d ' ' -f 3 | tr -d '\n'",
                shell=True, stdout=subprocess.PIPE)

            addr = proc.stdout.read()
            return addr.decode("UTF-8")
        except Exception as e:
            logger.error(e)

            return None

    def check_host(self, host, port):
        logger.debug("checking %s:%d" % (host, port))

        (_, h) = self.agent.request_header(
            self.agent.make_host_url(host, "/"), "GET", "Server")

        return h

    def announce_sensor(self, e):
        logger.debug("announcing sensor to the agent")
        s = None
        pid = os.getpid()
        cmdline = []

        try:
            if os.path.isfile("/proc/self/cmdline"):
                with open("/proc/self/cmdline") as cmd:
                    cmdinfo = cmd.read()
                cmdline = cmdinfo.split('\x00')
            else:
                # Python doesn't provide a reliable method to determine what
                # the OS process command line may be.  Here we are forced to
                # rely on ps rather than adding a dependency on something like
                # psutil which requires dev packages, gcc etc...
                proc = subprocess.Popen(["ps", "-p", str(pid), "-o", "command"],
                                        stdout=subprocess.PIPE)
                (out, err) = proc.communicate()
                parts = out.split(b'\n')
                cmdline = [parts[1].decode("utf-8")]
        except Exception:
            cmdline = sys.argv
            logger.debug("announce_sensor", exc_info=True)

        d = Discovery(pid=self.__get_real_pid(),
                      name=cmdline[0],
                      args=cmdline[1:])

        # If we're on a system with a procfs
        if os.path.exists("/proc/"):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((self.agent.host, 42699))
            path = "/proc/%d/fd/%d" % (pid, s.fileno())
            d.fd = s.fileno()
            d.inode = os.readlink(path)

        (b, _) = self.agent.request_response(
            self.agent.make_url(AGENT_DISCOVERY_URL), "PUT", d)
        if b:
            self.agent.set_from(b)
            self.fsm.ready()
            logger.info("Host agent available. We're in business. Announced pid: %s (true pid: %s)" %
                        (str(pid), str(self.agent.from_.pid)))
            return True
        else:
            logger.debug("Cannot announce sensor. Scheduling retry.")
            self.schedule_retry(self.announce_sensor, e, "announce")
        return False

    def schedule_retry(self, fun, e, name):
        logger.debug("Scheduling: " + name)
        self.timer = t.Timer(self.RETRY_PERIOD, fun, [e])
        self.timer.daemon = True
        self.timer.name = name
        self.timer.start()

    def test_agent(self, e):
        logger.debug("testing communication with the agent")

        (b, _) = self.agent.head(self.agent.make_url(AGENT_DATA_URL))

        if not b:
            self.schedule_retry(self.test_agent, e, "agent test")
        else:
            self.fsm.test()

    def __get_real_pid(self):
        """
        Attempts to determine the true process ID by querying the
        /proc/<pid>/sched file.  This works on systems with a proc filesystem.
        Otherwise default to os default.
        """
        pid = None

        if os.path.exists("/proc/"):
            sched_file = "/proc/%d/sched" % os.getpid()

            if os.path.isfile(sched_file):
                try:
                    file = open(sched_file)
                    line = file.readline()
                    g = re.search(r'\((\d+),', line)
                    if len(g.groups()) == 1:
                        pid = int(g.groups()[0])
                except Exception:
                    logger.debug("parsing sched file failed", exc_info=True)
                    pass

        if pid is None:
            pid = os.getpid()

        return pid

    def __get_agent_host_port(self):
        """
        Iterates the the various ways the host and port of the Instana host
        agent may be configured: default, env vars, sensor options...
        """
        host = AGENT_DEFAULT_HOST
        port = AGENT_DEFAULT_PORT

        if "INSTANA_AGENT_HOST" in os.environ:
            host = os.environ["INSTANA_AGENT_HOST"]
            if "INSTANA_AGENT_PORT" in os.environ:
                port = int(os.environ["INSTANA_AGENT_PORT"])

        elif "INSTANA_AGENT_IP" in os.environ:
            # Deprecated: INSTANA_AGENT_IP environment variable
            # To be removed in a future version
            host = os.environ["INSTANA_AGENT_IP"]
            if "INSTANA_AGENT_PORT" in os.environ:
                port = int(os.environ["INSTANA_AGENT_PORT"])

        elif self.agent.sensor.options.agent_host != "":
            host = self.agent.sensor.options.agent_host
            if self.agent.sensor.options.agent_port != 0:
                port = self.agent.sensor.options.agent_port

        return host, port
