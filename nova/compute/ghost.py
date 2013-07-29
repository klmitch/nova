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

"""
Represent ghosts of instances past

Some virt drivers have to perform miscellaneous cleaning activities
after deleting an instance before the resources used by that instance
may be reused.  Nova needs to be able to track these temporarily
unavailable resources to avoid scheduling new instances to those
compute nodes until the compute nodes are ready.  This temporary
resource hold is tracked using a Ghost.  Ghosts have expiration times,
and are only relevant to the compute node on which they reside.
"""

import collections
import datetime

import six

from nova.openstack.common import timeutils


class Ghost(object):
    """
    Describes resources originally held by an instance and temporarily
    being held for scrubbing activities.  Also contains an expiration
    timestamp.
    """

    def __init__(self, expire, **resources):
        """
        Initialize a ghost.

        :param expire: A datetime object or the number of seconds in
                       the future at which this ghost should expire.

        All keyword parameters identify resource names and their
        integer values, which will be held reserved until the ghost
        expires.
        """

        # If expiration is a number, convert it to a delta
        if (isinstance(expire, six.integer_types) or
                isinstance(expire, float)):
            expire = datetime.timedelta(seconds=expire)

        # If expiration is a delta (possibly arising from the above),
        # add it to the current time
        if isinstance(expire, datetime.timedelta):
            expire = timeutils.utcnow() + expire

        self.expire_ts = expire
        self.resources = resources


class GhostSet(object):
    """
    Describes a set of unexpired ghosts and reports the resources
    reserved by them.
    """

    def __init__(self):
        """
        Initialize a ghost set.
        """

        self.ghosts = []
        self._next_expire = None
        self._resources = None

    def add(self, ghost):
        """
        Adds a ghost to the set.

        :param ghost: The ghost to add to the set.
        """

        self.ghosts.append(ghost)

        # Keep track of the next expiration time
        self._next_expire = min(gh.expire_ts for gh in self.ghosts)

        # If the resources cache has been set up, update it
        if self._resources is not None:
            for key, value in ghost.resources.items():
                self._resources[key] += value

    @property
    def resources(self):
        """
        A dictionary of reserved resources.
        """

        # Check to see if we need to recompute the cache
        current_time = timeutils.utcnow()
        if (self._next_expire is not None and
                self._next_expire <= current_time):
            # Drop the expired ghosts
            self.ghosts = [gh for gh in self.ghosts
                           if gh.expire_ts > current_time]

            # Calculate the next expiration time
            self._next_expire = (min(gh.expire_ts for gh in self.ghosts)
                                 if self.ghosts else None)

            # Clear the cache; this will cause it to be recomputed
            self._resources = None

        # Recompute the resources cache if needed
        if self._resources is None:
            self._resources = collections.defaultdict(int)

            for ghost in self.ghosts:
                for key, value in ghost.resources.items():
                    self._resources[key] += value

        return self._resources
