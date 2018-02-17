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
import select
import json
import time
from six.moves import queue as Queue
import logging
import pprint
import voluptuous as v

from zuul.connection import BaseConnection
from zuul.model import TriggerEvent


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
        event.type = data.get('type')
        event.trigger_name = 'GitHub'
        change = data.get('change')

        if change:
            event.project_name = change.get('project')
            event.branch = change.get('branch')
            event.change_number = str(change.get('number'))
            event.change_url = change.get('url')
            patchset = data.get('patchSet')
            if patchset:
                event.patch_number = patchset.get('number')
                event.refspec = patchset.get('ref')
            event.approvals = data.get('approvals', [])
            event.comment = data.get('comment')
        refupdate = data.get('refUpdate')
        if refupdate:
            event.project_name = refupdate.get('project')
            event.ref = refupdate.get('refName')
            if (self.connection.strip_branch_ref and
                event.ref.startswith('refs/heads/')):
                event.ref = event.ref[len('refs/heads/'):]
            event.oldrev = refupdate.get('oldRev')
            event.newrev = refupdate.get('newRev')
        # Map the event types to a field name holding a Gerrit
        # account attribute. See Gerrit stream-event documentation
        # in cmd-stream-events.html
        accountfield_from_type = {
            'patchset-created': 'uploader',
            'draft-published': 'uploader',  # Gerrit 2.5/2.6
            'change-abandoned': 'abandoner',
            'change-restored': 'restorer',
            'change-merged': 'submitter',
            'merge-failed': 'submitter',  # Gerrit 2.5/2.6
            'comment-added': 'author',
            'ref-updated': 'submitter',
            'reviewer-added': 'reviewer',  # Gerrit 2.5/2.6
        }
        try:
            event.account = data.get(accountfield_from_type[event.type])
        except KeyError:
            self.log.warning("Received unrecognized event type '%s' from GitHub.\
                    Can not get account information." % event.type)
            event.account = None

        if (event.change_number and
            self.connection.sched.getProject(event.project_name)):
            # Call _getChange for the side effect of updating the
            # cache.  Note that this modifies Change objects outside
            # the main thread.
            # NOTE(jhesketh): Ideally we'd just remove the change from the
            # cache to denote that it needs updating. However the change
            # object is already used by Item's and hence BuildSet's etc. and
            # we need to update those objects by reference so that they have
            # the correct/new information and also avoid hitting GitHub
            # multiple times.
            if self.connection.attached_to['source']:
                self.connection.attached_to['source'][0]._getChange(
                    event.change_number, event.patch_number, refresh=True)
                # We only need to do this once since the connection maintains
                # the cache (which is shared between all the sources)
                # NOTE(jhesketh): We may couple sources and connections again
                # at which point this becomes more sensible.
        self.connection.sched.addEvent(event)

    def run(self):
        while True:
            if self._stopped:
                return
            try:
                self._handleEvent()
            except:
                self.log.exception("Exception moving GitHub event:")
            finally:
                self.connection.eventDone()


class GitHubWatcher(threading.Thread):
    log = logging.getLogger("GitHub.GitHubWatcher")
    poll_timeout = 500

    def __init__(self, github_connection, username, hostname, port=29418,
                 keyfile=None, keepalive=60):
        threading.Thread.__init__(self)
        self.username = username
        self.keyfile = keyfile
        self.hostname = hostname
        self.port = port
        self.github_connection = github_connection
        self.keepalive = keepalive
        self._stopped = False

    def _read(self, fd):
        l = fd.readline()
        data = json.loads(l)
        self.log.debug("Received data from GitHub event stream: \n%s" %
                       pprint.pformat(data))
        self.github_connection.addEvent(data)

    def _listen(self, stdout, stderr):
        poll = select.poll()
        poll.register(stdout.channel)
        while not self._stopped:
            ret = poll.poll(self.poll_timeout)
            for (fd, event) in ret:
                if fd == stdout.channel.fileno():
                    if event == select.POLLIN:
                        self._read(stdout)
                    else:
                        raise Exception("event on ssh connection")

    def _run(self):
        try:
        except:
            self.log.exception("Exception on webhook event stream:")
            time.sleep(5)
        finally:
            # If we don't close on exceptions to connect we can leak the
            # connection and DoS GitHub.
            client.close()

    def run(self):
        while not self._stopped:
            self._run()

    def stop(self):
        self.log.debug("Stopping watcher")
        self._stopped = True


class GitHubConnection(BaseConnection):
    driver_name = 'github'
    log = logging.getLogger("zuul.GitHubConnection")

    def __init__(self, connection_name, connection_config):
        super(GitHubConnection, self).__init__(connection_name,
                                               connection_config)
        if 'server' not in self.connection_config:
            raise Exception('server is required for github connections in '
                            '%s' % self.connection_name)
        if 'user' not in self.connection_config:
            raise Exception('user is required for github connections in '
                            '%s' % self.connection_name)

        self.user = self.connection_config.get('user')
        self.server = self.connection_config.get('server')
        self.port = int(self.connection_config.get('port', 29418))
        self.keyfile = self.connection_config.get('sshkey', None)
        self.keepalive = int(self.connection_config.get('keepalive', 60))
        self.strip_branch_ref = bool(self.connection_config.get(
            'strip_branch_ref'))
        self.watcher_thread = None
        self.event_queue = None
        self.client = None

        self.baseurl = self.connection_config.get('baseurl',
                                                  'https://%s' % self.server)

        self._change_cache = {}
        self.github_event_connector = None

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

    def _open(self):
        try:
            self.client = client
        except Exception:
            client.close()
            self.client = None
            raise

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
            self.server,
            self.port,
            keyfile=self.keyfile,
            keepalive=self.keepalive)
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
