# test_mailmap.py -- Tests for dulwich.mailmap
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
#
# SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later
# Dulwich is dual-licensed under the Apache License, Version 2.0 and the GNU
# General Public License as published by the Free Software Foundation; version 2.0
# or (at your option) any later version. You can redistribute it and/or
# modify it under the terms of either of these two licenses.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# You should have received a copy of the licenses; if not, see
# <http://www.gnu.org/licenses/> for a copy of the GNU General Public License
# and <http://www.apache.org/licenses/LICENSE-2.0> for a copy of the Apache
# License, Version 2.0.
#

"""Tests for dulwich.mailmap."""

from io import BytesIO
from unittest import TestCase

from dulwich.mailmap import Mailmap, read_mailmap


class ReadMailmapTests(TestCase):
    def test_read(self) -> None:
        b = BytesIO(
            b"""\
Jane Doe         <jane@desktop.(none)>
Joe R. Developer <joe@example.com>
# A comment
<cto@company.xx>                       <cto@coompany.xx> # Comment
Some Dude <some@dude.xx>         nick1 <bugs@company.xx>
Other Author <other@author.xx>   nick2 <bugs@company.xx>
Other Author <other@author.xx>         <nick2@company.xx>
Santa Claus <santa.claus@northpole.xx> <me@company.xx>
"""
        )
        self.assertEqual(
            [
                ((b"Jane Doe", b"jane@desktop.(none)"), None),
                ((b"Joe R. Developer", b"joe@example.com"), None),
                ((None, b"cto@company.xx"), (None, b"cto@coompany.xx")),
                (
                    (b"Some Dude", b"some@dude.xx"),
                    (b"nick1", b"bugs@company.xx"),
                ),
                (
                    (b"Other Author", b"other@author.xx"),
                    (b"nick2", b"bugs@company.xx"),
                ),
                (
                    (b"Other Author", b"other@author.xx"),
                    (None, b"nick2@company.xx"),
                ),
                (
                    (b"Santa Claus", b"santa.claus@northpole.xx"),
                    (None, b"me@company.xx"),
                ),
            ],
            list(read_mailmap(b)),
        )


class MailmapTests(TestCase):
    def test_lookup(self) -> None:
        m = Mailmap()
        m.add_entry((b"Jane Doe", b"jane@desktop.(none)"), (None, None))
        m.add_entry((b"Joe R. Developer", b"joe@example.com"), None)
        m.add_entry((None, b"cto@company.xx"), (None, b"cto@coompany.xx"))
        m.add_entry((b"Some Dude", b"some@dude.xx"), (b"nick1", b"bugs@company.xx"))
        m.add_entry(
            (b"Other Author", b"other@author.xx"),
            (b"nick2", b"bugs@company.xx"),
        )
        m.add_entry((b"Other Author", b"other@author.xx"), (None, b"nick2@company.xx"))
        m.add_entry(
            (b"Santa Claus", b"santa.claus@northpole.xx"),
            (None, b"me@company.xx"),
        )
        self.assertEqual(
            b"Jane Doe <jane@desktop.(none)>",
            m.lookup(b"Jane Doe <jane@desktop.(none)>"),
        )
        self.assertEqual(
            b"Jane Doe <jane@desktop.(none)>",
            m.lookup(b"Jane Doe <jane@example.com>"),
        )
        self.assertEqual(
            b"Jane Doe <jane@desktop.(none)>",
            m.lookup(b"Jane D. <jane@desktop.(none)>"),
        )
        self.assertEqual(
            b"Some Dude <some@dude.xx>", m.lookup(b"nick1 <bugs@company.xx>")
        )
        self.assertEqual(b"CTO <cto@company.xx>", m.lookup(b"CTO <cto@coompany.xx>"))

    def test_lookup_with_identity_tuple(self) -> None:
        """Test lookup using an identity tuple instead of a string."""
        m = Mailmap()
        m.add_entry(
            (b"Real Name", b"real@example.com"), (b"Alias", b"alias@example.com")
        )

        # Test lookup with a tuple
        self.assertEqual(
            (b"Real Name", b"real@example.com"),
            m.lookup((b"Alias", b"alias@example.com")),
        )

        # Test lookup with another tuple that doesn't match anything
        self.assertEqual(
            (b"Unknown", b"unknown@example.com"),
            m.lookup((b"Unknown", b"unknown@example.com")),
        )

    def test_lookup_with_no_match(self) -> None:
        """Test lookup when no match is found."""
        m = Mailmap()
        m.add_entry(
            (b"Real Name", b"real@example.com"), (b"Alias", b"alias@example.com")
        )

        # No match should return the original identity
        original = b"Unknown <unknown@example.com>"
        self.assertEqual(original, m.lookup(original))

    def test_lookup_partial_matches(self) -> None:
        """Test lookup with partial matches (name or email only)."""
        m = Mailmap()
        # Add entry with only name
        m.add_entry((b"Real Name", None), (b"Any Name", None))
        # Add entry with only email
        m.add_entry((None, b"real@example.com"), (None, b"other@example.com"))

        # Match by name
        self.assertEqual(
            b"Real Name <any@example.com>", m.lookup(b"Any Name <any@example.com>")
        )

        # Match by email
        self.assertEqual(
            b"Any Name <real@example.com>", m.lookup(b"Any Name <other@example.com>")
        )

    def test_add_entry_name_or_email_only(self) -> None:
        """Test adding entries with only name or only email."""
        m = Mailmap()

        # Entry with only canonical name
        m.add_entry((b"Real Name", None), (b"Alias", b"alias@example.com"))

        # Entry with only canonical email
        m.add_entry((None, b"real@example.com"), (b"Other", b"other@example.com"))

        # Lookup should properly combine the identity parts
        self.assertEqual(
            b"Real Name <alias@example.com>", m.lookup(b"Alias <alias@example.com>")
        )

        self.assertEqual(
            b"Other <real@example.com>", m.lookup(b"Other <other@example.com>")
        )
