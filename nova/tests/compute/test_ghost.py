# vim: tabstop=6 shiftwidth=4 softtabstop=4

# Copyright 2013 Openstack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import collections
import datetime

from nova.compute import ghost
from nova.openstack.common import timeutils
from nova import test


class GhostTestCase(test.TestCase):
    def test_init_datetime(self):
        now = timeutils.utcnow()
        gh = ghost.Ghost(now, a=1, b=2, c=3)

        self.assertEqual(gh.expire_ts, now)
        self.assertEqual(gh.resources, dict(a=1, b=2, c=3))

    def test_init_timedelta(self):
        self.useFixture(test.TimeOverride())
        now = timeutils.utcnow()
        delta = datetime.timedelta(seconds=15)
        gh = ghost.Ghost(delta, a=1, b=2, c=3)

        self.assertEqual(gh.expire_ts, now + delta)
        self.assertEqual(gh.resources, dict(a=1, b=2, c=3))

    def test_init_integer(self):
        self.useFixture(test.TimeOverride())
        now = timeutils.utcnow()
        delta = datetime.timedelta(seconds=15)
        gh = ghost.Ghost(15, a=1, b=2, c=3)

        self.assertEqual(gh.expire_ts, now + delta)
        self.assertEqual(gh.resources, dict(a=1, b=2, c=3))

    def test_init_float(self):
        self.useFixture(test.TimeOverride())
        now = timeutils.utcnow()
        delta = datetime.timedelta(seconds=1.5)
        gh = ghost.Ghost(1.5, a=1, b=2, c=3)

        self.assertEqual(gh.expire_ts, now + delta)
        self.assertEqual(gh.resources, dict(a=1, b=2, c=3))


class GhostSetTestCase(test.TestCase):
    def test_init(self):
        gs = ghost.GhostSet()

        self.assertEqual(gs.ghosts, [])
        self.assertEqual(gs._next_expire, None)
        self.assertEqual(gs._resources, None)

    def test_add_nocache(self):
        now = timeutils.utcnow()
        expire1 = now + datetime.timedelta(seconds=6)
        expire2 = now + datetime.timedelta(seconds=4)

        gs = ghost.GhostSet()
        gh1 = ghost.Ghost(expire1, a=1, b=2)
        gh2 = ghost.Ghost(expire2, a=2, c=4)

        gs.add(gh1)

        self.assertEqual(gs.ghosts, [gh1])
        self.assertEqual(gs._next_expire, expire1)
        self.assertEqual(gs._resources, None)

        # NOTE(Vek): Also verifies that second expire (being younger)
        #            becomes _next_expire
        gs.add(gh2)

        self.assertEqual(gs.ghosts, [gh1, gh2])
        self.assertEqual(gs._next_expire, expire2)
        self.assertEqual(gs._resources, None)

    def test_add_withcache(self):
        now = timeutils.utcnow()
        expire1 = now + datetime.timedelta(seconds=4)
        expire2 = now + datetime.timedelta(seconds=6)

        gs = ghost.GhostSet()
        gs._resources = collections.defaultdict(int)
        gh1 = ghost.Ghost(expire1, a=1, b=2)
        gh2 = ghost.Ghost(expire2, a=2, c=4)

        gs.add(gh1)

        self.assertEqual(gs.ghosts, [gh1])
        self.assertEqual(gs._next_expire, expire1)
        self.assertEqual(gs._resources, dict(a=1, b=2))

        # NOTE(Vek): Also verifies that first expire (being younger)
        #            stays _next_expire
        gs.add(gh2)

        self.assertEqual(gs.ghosts, [gh1, gh2])
        self.assertEqual(gs._next_expire, expire1)
        self.assertEqual(gs._resources, dict(a=3, b=2, c=4))

    def test_resources_cached_noexpires(self):
        gs = ghost.GhostSet()
        gs._resources = 'cached'

        self.assertEqual(gs.resources, 'cached')

    def test_resources_uncached_noexpires(self):
        self.useFixture(test.TimeOverride())
        now = timeutils.utcnow()
        expire1 = now + datetime.timedelta(seconds=4)
        expire2 = now + datetime.timedelta(seconds=6)

        gs = ghost.GhostSet()
        gh1 = ghost.Ghost(expire1, a=1, b=2)
        gh2 = ghost.Ghost(expire2, a=2, c=4)
        gs.ghosts = [gh1, gh2]
        gs._next_expire = expire1

        self.assertEqual(gs.resources, dict(a=3, b=2, c=4))

    def test_resources_expires(self):
        self.useFixture(test.TimeOverride())
        now = timeutils.utcnow()
        expire1 = now + datetime.timedelta(seconds=-2)
        expire2 = now + datetime.timedelta(seconds=2)

        gs = ghost.GhostSet()
        gh1 = ghost.Ghost(expire1, a=1, b=2)
        gh2 = ghost.Ghost(expire2, a=2, c=4)
        gs.ghosts = [gh1, gh2]
        gs._next_expire = expire1
        gs._resources = 'cached'

        self.assertEqual(gs.resources, dict(a=2, c=4))
        self.assertEqual(gs.ghosts, [gh2])
        self.assertEqual(gs._next_expire, expire2)

        timeutils.advance_time_seconds(3)

        self.assertEqual(gs.resources, {})
        self.assertEqual(gs.ghosts, [])
        self.assertEqual(gs._next_expire, None)
