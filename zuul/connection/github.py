# Copyright 2011 OpenStack, LLC.
# Copyright 2012 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import threading
import time
from six.moves import queue as Queue
import logging
import voluptuous as v

from aiohttp import web
from aiohttp_threaded import AIOHttpThreaded

from zuul.connection import BaseConnection
from zuul.model import TriggerEvent


# TODO(hds)
class GitHubEventConnector(threading.Thread):
    """Move events from GitHub to the scheduler."""

    log = logging.getLogger("zuul.GitHubEventConnector")
    delay = 10.0

    def __init__(self, connection):
        super(GitHubEventConnector, self).__init__()
        self.daemon = True
        self.connection = connection
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.connection.addEvent(None)

    def _handleEvent(self):
        ts, data = self.connection.getEvent()

        if self._stopped:
            return

        event = TriggerEvent()
        event.type = data['change']
        event.trigger_name = 'GitHub'
        request = data['request']

        if event.type == "push":
            event.full_name = request["repository"]["full_name"]
            event.sha = request["payload"]["after"]
            self.log.info("full_name: %s, sha: %s" % (event.full_name, event.sha))
        else:
            self.log.warning("Received unrecognized event type '%s' from GitHub.\
                    Can not get account information." % event.type)

        self.connection.sched.addEvent(event)

    def run(self):
        while True:
            if self._stopped:
                return
            # noinspection PyBroadException
            try:
                self._handleEvent()
            except:
                self.log.exception("Exception moving GitHub event:")
            finally:
                self.connection.eventDone()


class GitHubWatcher(AIOHttpThreaded):
    log = logging.getLogger("github.GitHubWatcher")
    poll_timeout = 500

    def __init__(self, github_connection, user, token, listen_address, listen_port, event_loop):
        super(GitHubWatcher, self).__init__(listen_address, listen_port, event_loop)
        self._event_loop = event_loop
        self.username = user
        self.token = token
        self.github_connection = github_connection
        self._stopped = False

    async def post_root(self, request):
        self.log.debug("github.GitHubWatcher.post_root")

        status_code = 200
        text = "OK"

        if "X-GITHUB-EVENT" in request.headers:
            if request.headers["X-GITHUB-EVENT"] == "push":
                post_request = await request.post()
                data = {"change": "push", "request": post_request}
                self.log.debug("Received data from GitHub webhook: \n%s" % data)
                self.github_connection.addEvent(data)

        else:
            status_code = 404
            text = "NOK"

        return web.Response(status=status_code, text=text)

    async def get_hello(self, request):
        self.log.debug("github.GitHubWatcher.get_hello")
        name = request.match_info.get("name", "Anonymous")
        text = f"Hello {name}!"
        return web.Response(text=text)


class GitHubConnection(BaseConnection):
    driver_name = 'github'
    log = logging.getLogger("zuul.GitHubConnection")

    def __init__(self, connection_name, connection_config):
        super(GitHubConnection, self).__init__(connection_name,
                                               connection_config)
        if 'user' not in self.connection_config:
            raise Exception('user is required for github connections in '
                            '%s' % self.connection_name)

        if 'token' not in self.connection_config:
            raise Exception('token is required for github connections in '
                            '%s' % self.connection_name)

        self.user = self.connection_config.get('user')
        self.token = self.connection_config.get('token')
        self.listen_address = self.connection_config.get('listen_address', '127.0.0.1')
        self.listen_port = int(self.connection_config.get('listen_port', 8989))

        self._change_cache = {}
        self.github_event_connector = None
        self._routes = None

    def getCachedChange(self, key):
        if key in self._change_cache:
            return self._change_cache.get(key)
        return None

    def updateChangeCache(self, key, value):
        self._change_cache[key] = value

    def deleteCachedChange(self, key):
        if key in self._change_cache:
            del self._change_cache[key]

    def maintainCache(self, relevant):
        # This lets the user supply a list of change objects that are
        # still in use.  Anything in our cache that isn't in the supplied
        # list should be safe to remove from the cache.
        remove = []
        for key, change in list(self._change_cache.items()):
            if change not in relevant:
                remove.append(key)
        for key in remove:
            del self._change_cache[key]

    def addEvent(self, data):
        return self.event_queue.put((time.time(), data))

    def getEvent(self):
        return self.event_queue.get()

    def eventDone(self):
        self.event_queue.task_done()

    def onLoad(self):
        self.log.debug("Starting GitHub Conncetion/Watchers")
        self._start_watcher_thread()
        self._start_event_connector()

    def onStop(self):
        self.log.debug("Stopping GitHub Conncetion/Watchers")
        self._stop_watcher_thread()
        self._stop_event_connector()

    def _stop_watcher_thread(self):
        if self.watcher_thread:
            self.watcher_thread.stop()
            self.watcher_thread.join()

    def _start_watcher_thread(self):
        self.event_queue = Queue.Queue()

        self.watcher_thread = GitHubWatcher(
            self,
            self.user,
            self.token,
            self.listen_address,
            self.listen_port)

        self._routes = [web.post("/", self.watcher_thread.post_root),
                        web.get("/hello/{name}", self.watcher_thread.get_hello)]
        self.watcher_thread.add_routes(self._routes)
        self.watcher_thread.start()

    def _stop_event_connector(self):
        if self.github_event_connector:
            self.github_event_connector.stop()
            self.github_event_connector.join()

    def _start_event_connector(self):
        self.github_event_connector = GitHubEventConnector(self)
        self.github_event_connector.start()


def getSchema():
    github_connection = v.Any(str, v.Schema(dict))
    return github_connection
