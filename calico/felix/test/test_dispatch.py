# -*- coding: utf-8 -*-
# Copyright 2014, 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
felix.test.test_dispatch
~~~~~~~~~~~~~~~~~~~~~~~~

Tests of the actor that controls the top-level dispatch chain.
"""
from pprint import pformat

import itertools
import mock

from calico.felix.test.base import BaseTestCase, load_config
from calico.felix.dispatch import DispatchChains, _find_longest_prefix
from calico.felix.frules import CHAIN_TO_ENDPOINT, CHAIN_FROM_ENDPOINT

class TestDispatchChains(BaseTestCase):
    """
    Tests for the DispatchChains actor.
    """
    def setUp(self):
        super(TestDispatchChains, self).setUp()
        self.iptables_updater = mock.MagicMock()
        self.config = load_config("felix_default.cfg", global_dict={
            "MetadataPort": "8775"})

    def getDispatchChain(self):
        return DispatchChains(
            config=self.config,
            ip_version=4,
            iptables_updater=self.iptables_updater
        )

    def assert_iptables_update(self,
                               args,
                               to_updates,
                               from_updates,
                               to_chain_names,
                               from_chain_names):
        # We only care about positional arguments
        args = args[0]

        # The DispatchChains object stores the endpoints in a set, which means
        # that when it builds the list of goto rules they can be emitted in any
        # order. However, the DROP rule must always appear at the end. To do
        # that, first check that the updates contain the same rules in any
        # order (using assertItemsEqual), and then confirm that the last rule
        # is the DROP rule.
        self.assertItemsEqual(args[0][CHAIN_TO_ENDPOINT], to_updates)
        self.assertItemsEqual(args[0][CHAIN_FROM_ENDPOINT], from_updates)
        self.assertEqual(args[0][CHAIN_TO_ENDPOINT][-1], to_updates[-1])
        self.assertEqual(args[0][CHAIN_FROM_ENDPOINT][-1], from_updates[-1])

        # Confirm that the dependency sets match.
        self.assertEqual(args[1][CHAIN_TO_ENDPOINT], to_chain_names)
        self.assertEqual(args[1][CHAIN_FROM_ENDPOINT], from_chain_names)

    def test_applying_metadata(self):
        """
        Tests that a snapshot with metadata works OK.
        """
        self.config = load_config("felix_default.cfg", global_dict={
            "MetadataPort": "8775",
            "MetadataAddr": "127.0.0.1"})

        d = self.getDispatchChain()

        ifaces = ['tapabcdef', 'tap123456', 'tapb7d849']
        d.apply_snapshot(ifaces, async=True)
        self.step_actor(d)

        from_updates = [
            '--append felix-FROM-ENDPOINT --in-interface tapabcdef --goto felix-from-abcdef',
            '--append felix-FROM-ENDPOINT --in-interface tap123456 --goto felix-from-123456',
            '--append felix-FROM-ENDPOINT --in-interface tapb7d849 --goto felix-from-b7d849',
            '--append felix-FROM-ENDPOINT --jump DROP -m comment '
            '--comment "From unknown endpoint"',
        ]
        to_updates = [
            '--append felix-TO-ENDPOINT --out-interface tapabcdef --goto felix-to-abcdef',
            '--append felix-TO-ENDPOINT --out-interface tap123456 --goto felix-to-123456',
            '--append felix-TO-ENDPOINT --out-interface tapb7d849 --goto felix-to-b7d849',
            '--append felix-TO-ENDPOINT --jump DROP -m comment --comment "To unknown endpoint"',
        ]
        from_chain_names = set(['felix-from-abcdef', 'felix-from-123456', 'felix-from-b7d849'])
        to_chain_names = set(['felix-to-abcdef', 'felix-to-123456', 'felix-to-b7d849'])
        args = self.iptables_updater.rewrite_chains.call_args
        self.assert_iptables_update(
            args, to_updates, from_updates, to_chain_names,
            from_chain_names
        )

    def test_tree_building(self):
        d = self.getDispatchChain()
        d.programmed_leaf_chains.add("felix-FROM-EP-PFX-a")
        d.programmed_leaf_chains.add("felix-FROM-EP-PFX-z")
        ifaces = ['tapa1', 'tapa2', 'tapa3',
                  'tapb1', 'tapb20123456789012345',
                  'tapc']
        to_delete, deps, updates, new_leaf_chains = d._calculate_update(ifaces)
        self.assertEqual(to_delete, set(["felix-FROM-EP-PFX-z"]))
        print "Deps", pformat(deps)
        self.assertEqual(deps, {
            'felix-TO-ENDPOINT': set(
                ['felix-FROM-EP-PFX-a', 'felix-FROM-EP-PFX-b', 'felix-to-c']),
            'felix-FROM-ENDPOINT': set(
                ['felix-TO-EP-PFX-a', 'felix-TO-EP-PFX-b', 'felix-from-c']),

            'felix-TO-EP-PFX-a': set(['felix-to-a1',
                                      'felix-to-a2',
                                      'felix-to-a3']),
            'felix-TO-EP-PFX-b': set(['felix-to-b1',
                                      'felix-to-_62629f0db434d57']),

            'felix-FROM-EP-PFX-a': set(['felix-from-a1',
                                        'felix-from-a2',
                                        'felix-from-a3']),
            'felix-FROM-EP-PFX-b': set(['felix-from-b1',
                                        'felix-from-_62629f0db434d57']),
        })
        for chain_name, chain_updates in updates.items():
            chain_updates[:] = sorted(chain_updates[:-1]) + chain_updates[-1:]
        print "Updates:", pformat(updates)
        self.assertEqual(updates, {
            'felix-TO-ENDPOINT': [
                # If there are multiple endpoints with a prefix, we get a
                # prefix match.
                '--append felix-TO-ENDPOINT --out-interface tapa+ --goto felix-TO-EP-PFX-a',
                '--append felix-TO-ENDPOINT --out-interface tapb+ --goto felix-TO-EP-PFX-b',
                # If there's only one, we don't.
                '--append felix-TO-ENDPOINT --out-interface tapc --goto felix-to-c',
                '--append felix-TO-ENDPOINT --jump DROP -m comment --comment "To unknown endpoint"'],
            'felix-FROM-ENDPOINT': [
                '--append felix-FROM-ENDPOINT --in-interface tapa+ --goto felix-FROM-EP-PFX-a',
                '--append felix-FROM-ENDPOINT --in-interface tapb+ --goto felix-FROM-EP-PFX-b',
                '--append felix-FROM-ENDPOINT --in-interface tapc --goto felix-from-c',
                '--append felix-FROM-ENDPOINT --jump DROP -m comment --comment "From unknown endpoint"'],
            'felix-FROM-EP-PFX-a': [
                # Per-prefix chain has one entry per endpoint.
                '--append felix-FROM-EP-PFX-a --in-interface tapa1 --goto felix-from-a1',
                '--append felix-FROM-EP-PFX-a --in-interface tapa2 --goto felix-from-a2',
                '--append felix-FROM-EP-PFX-a --in-interface tapa3 --goto felix-from-a3',
                # And a trailing drop.
                '--append felix-FROM-EP-PFX-a --jump DROP -m comment --comment "From unknown endpoint"'],
            'felix-FROM-EP-PFX-b': [
                '--append felix-FROM-EP-PFX-b --in-interface tapb1 --goto felix-from-b1',
                '--append felix-FROM-EP-PFX-b --in-interface tapb20123456789012345 --goto felix-from-_62629f0db434d57',
                '--append felix-FROM-EP-PFX-b --jump DROP -m comment --comment "From unknown endpoint"'],
            'felix-TO-EP-PFX-a': [
                '--append felix-TO-EP-PFX-a --out-interface tapa1 --goto felix-to-a1',
                '--append felix-TO-EP-PFX-a --out-interface tapa2 --goto felix-to-a2',
                '--append felix-TO-EP-PFX-a --out-interface tapa3 --goto felix-to-a3',
                '--append felix-TO-EP-PFX-a --jump DROP -m comment --comment "To unknown endpoint"'],
            'felix-TO-EP-PFX-b': [
                '--append felix-TO-EP-PFX-b --out-interface tapb1 --goto felix-to-b1',
                '--append felix-TO-EP-PFX-b --out-interface tapb20123456789012345 --goto felix-to-_62629f0db434d57',
                '--append felix-TO-EP-PFX-b --jump DROP -m comment --comment "To unknown endpoint"']
        })

    def test_applying_snapshot_clean(self):
        """
        Tests that a snapshot can be applied to a previously unused actor.
        """
        d = self.getDispatchChain()

        ifaces = ['tapabcdef', 'tap123456', 'tapb7d849']
        d.apply_snapshot(ifaces, async=True)
        self.step_actor(d)

        from_updates = [
            '--append felix-FROM-ENDPOINT --in-interface tapabcdef --goto felix-from-abcdef',
            '--append felix-FROM-ENDPOINT --in-interface tap123456 --goto felix-from-123456',
            '--append felix-FROM-ENDPOINT --in-interface tapb7d849 --goto felix-from-b7d849',
            '--append felix-FROM-ENDPOINT --jump DROP -m comment --comment "From unknown endpoint"',
        ]
        to_updates = [
            '--append felix-TO-ENDPOINT --out-interface tapabcdef --goto felix-to-abcdef',
            '--append felix-TO-ENDPOINT --out-interface tap123456 --goto felix-to-123456',
            '--append felix-TO-ENDPOINT --out-interface tapb7d849 --goto felix-to-b7d849',
            '--append felix-TO-ENDPOINT --jump DROP -m comment --comment "To unknown endpoint"',
        ]
        from_chain_names = set(['felix-from-abcdef', 'felix-from-123456', 'felix-from-b7d849'])
        to_chain_names = set(['felix-to-abcdef', 'felix-to-123456', 'felix-to-b7d849'])
        args = self.iptables_updater.rewrite_chains.call_args
        self.assert_iptables_update(
            args,
            to_updates,
            from_updates,
            to_chain_names,
            from_chain_names
        )

    def test_applying_snapshot_dirty(self):
        """
        Tests that a snapshot can be applied to an actor that used to have
        state.
        """
        d = self.getDispatchChain()

        # Insert some chains I don't want to see.
        d.apply_snapshot(['tapxyz', 'tap889900', 'tapundefined'], async=True)
        self.step_actor(d)

        ifaces = ['tapabcdef', 'tap123456', 'tapb7d849']
        d.apply_snapshot(ifaces, async=True)
        self.step_actor(d)

        from_updates = [
            '--append felix-FROM-ENDPOINT --in-interface tapabcdef --goto felix-from-abcdef',
            '--append felix-FROM-ENDPOINT --in-interface tap123456 --goto felix-from-123456',
            '--append felix-FROM-ENDPOINT --in-interface tapb7d849 --goto felix-from-b7d849',
            '--append felix-FROM-ENDPOINT --jump DROP -m comment --comment "From unknown endpoint"',
        ]
        to_updates = [
            '--append felix-TO-ENDPOINT --out-interface tapabcdef --goto felix-to-abcdef',
            '--append felix-TO-ENDPOINT --out-interface tap123456 --goto felix-to-123456',
            '--append felix-TO-ENDPOINT --out-interface tapb7d849 --goto felix-to-b7d849',
            '--append felix-TO-ENDPOINT --jump DROP -m comment --comment "To unknown endpoint"',
        ]
        from_chain_names = set(['felix-from-abcdef', 'felix-from-123456', 'felix-from-b7d849'])
        to_chain_names = set(['felix-to-abcdef', 'felix-to-123456', 'felix-to-b7d849'])

        self.assertEqual(self.iptables_updater.rewrite_chains.call_count, 2)
        args = self.iptables_updater.rewrite_chains.call_args
        self.assert_iptables_update(
            args,
            to_updates,
            from_updates,
            to_chain_names,
            from_chain_names
        )

    def test_applying_empty_snapshot(self):
        """
        Tests that an empty snapshot can be applied to an actor that used to
        have state.
        """
        d = self.getDispatchChain()

        # Insert some chains I don't want to see.
        d.apply_snapshot(['tapxyz', 'tap889900', 'tapundefined'], async=True)
        self.step_actor(d)

        # Clear it out
        d.apply_snapshot([], async=True)
        self.step_actor(d)

        from_updates = [
            '--append felix-FROM-ENDPOINT --jump DROP -m comment --comment "From unknown endpoint"',
        ]
        to_updates = [
            '--append felix-TO-ENDPOINT --jump DROP -m comment --comment "To unknown endpoint"',
        ]
        from_chain_names = set()
        to_chain_names = set()

        self.assertEqual(self.iptables_updater.rewrite_chains.call_count, 2)
        args = self.iptables_updater.rewrite_chains.call_args
        self.assert_iptables_update(
            args,
            to_updates,
            from_updates,
            to_chain_names,
            from_chain_names
        )

    def test_on_endpoint_added_simple(self):
        """
        Tests that adding an endpoint, adds it to the state.
        """
        d = self.getDispatchChain()

        # Insert some basic chains.
        d.apply_snapshot(['tapabcdef', 'tap123456'], async=True)
        self.step_actor(d)

        # Add one endpoint.
        d.on_endpoint_added('tapb7d849', async=True)
        self.step_actor(d)

        from_updates = [
            '--append felix-FROM-ENDPOINT --in-interface tapabcdef --goto felix-from-abcdef',
            '--append felix-FROM-ENDPOINT --in-interface tap123456 --goto felix-from-123456',
            '--append felix-FROM-ENDPOINT --in-interface tapb7d849 --goto felix-from-b7d849',
            '--append felix-FROM-ENDPOINT --jump DROP -m comment --comment "From unknown endpoint"',
        ]
        to_updates = [
            '--append felix-TO-ENDPOINT --out-interface tapabcdef --goto felix-to-abcdef',
            '--append felix-TO-ENDPOINT --out-interface tap123456 --goto felix-to-123456',
            '--append felix-TO-ENDPOINT --out-interface tapb7d849 --goto felix-to-b7d849',
            '--append felix-TO-ENDPOINT --jump DROP -m comment --comment "To unknown endpoint"',
        ]
        from_chain_names = set(['felix-from-abcdef', 'felix-from-123456', 'felix-from-b7d849'])
        to_chain_names = set(['felix-to-abcdef', 'felix-to-123456', 'felix-to-b7d849'])

        self.assertEqual(self.iptables_updater.rewrite_chains.call_count, 2)
        args = self.iptables_updater.rewrite_chains.call_args
        self.assert_iptables_update(
            args,
            to_updates,
            from_updates,
            to_chain_names,
            from_chain_names
        )

    def test_on_endpoint_added_idempotent(self):
        """
        Tests that adding an endpoint that's already present does nothing.
        """
        d = self.getDispatchChain()

        # Insert some basic chains.
        d.apply_snapshot(['tapabcdef', 'tap123456', 'tapb7d849'], async=True)
        self.step_actor(d)

        # Add an endpoint we already have.
        d.on_endpoint_added('tapabcdef', async=True)
        self.step_actor(d)

        # Confirm that we only got called once.
        self.assertEqual(self.iptables_updater.rewrite_chains.call_count, 1)

    def test_on_endpoint_removed_basic(self):
        """
        Tests that we can remove an endpoint.
        """
        d = self.getDispatchChain()

        # Insert some basic chains.
        d.apply_snapshot(['tapabcdef', 'tap123456', 'tapb7d849'], async=True)
        self.step_actor(d)

        # Remove an endpoint.
        d.on_endpoint_removed('tapabcdef', async=True)
        self.step_actor(d)

        from_updates = [
            '--append felix-FROM-ENDPOINT --in-interface tap123456 --goto felix-from-123456',
            '--append felix-FROM-ENDPOINT --in-interface tapb7d849 --goto felix-from-b7d849',
            '--append felix-FROM-ENDPOINT --jump DROP -m comment --comment "From unknown endpoint"',
        ]
        to_updates = [
            '--append felix-TO-ENDPOINT --out-interface tap123456 --goto felix-to-123456',
            '--append felix-TO-ENDPOINT --out-interface tapb7d849 --goto felix-to-b7d849',
            '--append felix-TO-ENDPOINT --jump DROP -m comment --comment "To unknown endpoint"',
        ]
        from_chain_names = set(['felix-from-123456', 'felix-from-b7d849'])
        to_chain_names = set(['felix-to-123456', 'felix-to-b7d849'])

        # Confirm that we got called twice.
        self.assertEqual(self.iptables_updater.rewrite_chains.call_count, 2)
        args = self.iptables_updater.rewrite_chains.call_args
        self.assert_iptables_update(
            args,
            to_updates,
            from_updates,
            to_chain_names,
            from_chain_names
        )

    def test_on_endpoint_removed_idempotent(self):
        """
        Tests that removing an endpoint multiple times does nothing.
        """
        d = self.getDispatchChain()

        # Insert some basic chains.
        d.apply_snapshot(['tapabcdef', 'tap123456', 'tapb7d849'], async=True)
        self.step_actor(d)

        # Remove an endpoint.
        d.on_endpoint_removed('tapabcdef', async=True)
        self.step_actor(d)

        # Remove it a few more times for good measure.
        d.on_endpoint_removed('tapabcdef', async=True)
        self.step_actor(d)
        d.on_endpoint_removed('tapabcdef', async=True)
        self.step_actor(d)
        d.on_endpoint_removed('tapabcdef', async=True)
        self.step_actor(d)

        # Confirm that we only got called twice.
        self.assertEqual(self.iptables_updater.rewrite_chains.call_count, 2)

    def test_longest_prefix(self):
        self.assertEqual(_find_longest_prefix([]), None)
        self.assertEqual(_find_longest_prefix(["a"]), "a")
        self.assertEqual(_find_longest_prefix(["a", ""]), "")
        self.assertEqual(_find_longest_prefix(["a", "ab"]), "a")
        self.assertEqual(_find_longest_prefix(["ab", "ab"]), "ab")
        self.assertEqual(_find_longest_prefix(["ab", "ab", "abc"]), "ab")
        self.assertEqual(_find_longest_prefix(["abc", "ab", "ab"]), "ab")
        self.assertEqual(_find_longest_prefix(["ab", "cd"]), "")
        self.assertEqual(_find_longest_prefix(["tapabcd", "tapacdef"]), "tapa")

    def assert_longest_prefix(self, strings, exp_prefix):
        for x in itertools.permutations(strings):
            self.assertEqual(_find_longest_prefix(strings), exp_prefix)

