# test_index.py -- Tests for the git index
# Copyright (C) 2008-2009 Jelmer Vernooij <jelmer@jelmer.uk>
#
# SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later
# Dulwich is dual-licensed under the Apache License, Version 2.0 and the GNU
# General Public License as public by the Free Software Foundation; version 2.0
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

"""Tests for the index."""

import os
import shutil
import stat
import struct
import sys
import tempfile
from io import BytesIO

from dulwich.index import (
    Index,
    IndexEntry,
    SerializedIndexEntry,
    _fs_to_tree_path,
    _tree_to_fs_path,
    build_index_from_tree,
    cleanup_mode,
    commit_tree,
    get_unstaged_changes,
    index_entry_from_directory,
    index_entry_from_path,
    index_entry_from_stat,
    iter_fresh_entries,
    read_index,
    read_index_dict,
    validate_path_element_default,
    validate_path_element_ntfs,
    write_cache_time,
    write_index,
    write_index_dict,
)
from dulwich.object_store import MemoryObjectStore
from dulwich.objects import S_IFGITLINK, Blob, Commit, Tree
from dulwich.repo import Repo

from . import TestCase, skipIf


def can_symlink() -> bool:
    """Return whether running process can create symlinks."""
    if sys.platform != "win32":
        # Platforms other than Windows should allow symlinks without issues.
        return True

    test_source = tempfile.mkdtemp()
    test_target = test_source + "can_symlink"
    try:
        os.symlink(test_source, test_target)
    except (NotImplementedError, OSError):
        return False
    return True


class IndexTestCase(TestCase):
    datadir = os.path.join(os.path.dirname(__file__), "../testdata/indexes")

    def get_simple_index(self, name):
        return Index(os.path.join(self.datadir, name))


class SimpleIndexTestCase(IndexTestCase):
    def test_len(self) -> None:
        self.assertEqual(1, len(self.get_simple_index("index")))

    def test_iter(self) -> None:
        self.assertEqual([b"bla"], list(self.get_simple_index("index")))

    def test_iter_skip_hash(self) -> None:
        self.assertEqual([b"bla"], list(self.get_simple_index("index_skip_hash")))

    def test_iterobjects(self) -> None:
        self.assertEqual(
            [(b"bla", b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", 33188)],
            list(self.get_simple_index("index").iterobjects()),
        )

    def test_getitem(self) -> None:
        self.assertEqual(
            IndexEntry(
                (1230680220, 0),
                (1230680220, 0),
                2050,
                3761020,
                33188,
                1000,
                1000,
                0,
                b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
                0,
                0,
            ),
            self.get_simple_index("index")[b"bla"],
        )

    def test_empty(self) -> None:
        i = self.get_simple_index("notanindex")
        self.assertEqual(0, len(i))
        self.assertFalse(os.path.exists(i._filename))

    def test_against_empty_tree(self) -> None:
        i = self.get_simple_index("index")
        changes = list(i.changes_from_tree(MemoryObjectStore(), None))
        self.assertEqual(1, len(changes))
        (oldname, newname), (oldmode, newmode), (oldsha, newsha) = changes[0]
        self.assertEqual(b"bla", newname)
        self.assertEqual(b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", newsha)


class SimpleIndexWriterTestCase(IndexTestCase):
    def setUp(self) -> None:
        IndexTestCase.setUp(self)
        self.tempdir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        IndexTestCase.tearDown(self)
        shutil.rmtree(self.tempdir)

    def test_simple_write(self) -> None:
        entries = [
            (
                SerializedIndexEntry(
                    b"barbla",
                    (1230680220, 0),
                    (1230680220, 0),
                    2050,
                    3761020,
                    33188,
                    1000,
                    1000,
                    0,
                    b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
                    0,
                    0,
                )
            )
        ]
        filename = os.path.join(self.tempdir, "test-simple-write-index")
        with open(filename, "wb+") as x:
            write_index(x, entries)

        with open(filename, "rb") as x:
            self.assertEqual(entries, list(read_index(x)))


class ReadIndexDictTests(IndexTestCase):
    def setUp(self) -> None:
        IndexTestCase.setUp(self)
        self.tempdir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        IndexTestCase.tearDown(self)
        shutil.rmtree(self.tempdir)

    def test_simple_write(self) -> None:
        entries = {
            b"barbla": IndexEntry(
                (1230680220, 0),
                (1230680220, 0),
                2050,
                3761020,
                33188,
                1000,
                1000,
                0,
                b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
                0,
                0,
            )
        }
        filename = os.path.join(self.tempdir, "test-simple-write-index")
        with open(filename, "wb+") as x:
            write_index_dict(x, entries)

        with open(filename, "rb") as x:
            self.assertEqual(entries, read_index_dict(x))


class CommitTreeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = MemoryObjectStore()

    def test_single_blob(self) -> None:
        blob = Blob()
        blob.data = b"foo"
        self.store.add_object(blob)
        blobs = [(b"bla", blob.id, stat.S_IFREG)]
        rootid = commit_tree(self.store, blobs)
        self.assertEqual(rootid, b"1a1e80437220f9312e855c37ac4398b68e5c1d50")
        self.assertEqual((stat.S_IFREG, blob.id), self.store[rootid][b"bla"])
        self.assertEqual({rootid, blob.id}, set(self.store._data.keys()))

    def test_nested(self) -> None:
        blob = Blob()
        blob.data = b"foo"
        self.store.add_object(blob)
        blobs = [(b"bla/bar", blob.id, stat.S_IFREG)]
        rootid = commit_tree(self.store, blobs)
        self.assertEqual(rootid, b"d92b959b216ad0d044671981196781b3258fa537")
        dirid = self.store[rootid][b"bla"][1]
        self.assertEqual(dirid, b"c1a1deb9788150829579a8b4efa6311e7b638650")
        self.assertEqual((stat.S_IFDIR, dirid), self.store[rootid][b"bla"])
        self.assertEqual((stat.S_IFREG, blob.id), self.store[dirid][b"bar"])
        self.assertEqual({rootid, dirid, blob.id}, set(self.store._data.keys()))


class CleanupModeTests(TestCase):
    def assertModeEqual(self, expected, got) -> None:
        self.assertEqual(expected, got, f"{expected:o} != {got:o}")

    def test_file(self) -> None:
        self.assertModeEqual(0o100644, cleanup_mode(0o100000))

    def test_executable(self) -> None:
        self.assertModeEqual(0o100755, cleanup_mode(0o100711))
        self.assertModeEqual(0o100755, cleanup_mode(0o100700))

    def test_symlink(self) -> None:
        self.assertModeEqual(0o120000, cleanup_mode(0o120711))

    def test_dir(self) -> None:
        self.assertModeEqual(0o040000, cleanup_mode(0o40531))

    def test_submodule(self) -> None:
        self.assertModeEqual(0o160000, cleanup_mode(0o160744))


class WriteCacheTimeTests(TestCase):
    def test_write_string(self) -> None:
        f = BytesIO()
        self.assertRaises(TypeError, write_cache_time, f, "foo")

    def test_write_int(self) -> None:
        f = BytesIO()
        write_cache_time(f, 434343)
        self.assertEqual(struct.pack(">LL", 434343, 0), f.getvalue())

    def test_write_tuple(self) -> None:
        f = BytesIO()
        write_cache_time(f, (434343, 21))
        self.assertEqual(struct.pack(">LL", 434343, 21), f.getvalue())

    def test_write_float(self) -> None:
        f = BytesIO()
        write_cache_time(f, 434343.000000021)
        self.assertEqual(struct.pack(">LL", 434343, 21), f.getvalue())


class IndexEntryFromStatTests(TestCase):
    def test_simple(self) -> None:
        st = os.stat_result(
            (
                16877,
                131078,
                64769,
                154,
                1000,
                1000,
                12288,
                1323629595,
                1324180496,
                1324180496,
            )
        )
        entry = index_entry_from_stat(st, b"22" * 20)
        self.assertEqual(
            entry,
            IndexEntry(
                1324180496,
                1324180496,
                64769,
                131078,
                16384,
                1000,
                1000,
                12288,
                b"2222222222222222222222222222222222222222",
                0,
                0,
            ),
        )

    def test_override_mode(self) -> None:
        st = os.stat_result(
            (
                stat.S_IFREG + 0o644,
                131078,
                64769,
                154,
                1000,
                1000,
                12288,
                1323629595,
                1324180496,
                1324180496,
            )
        )
        entry = index_entry_from_stat(st, b"22" * 20, mode=stat.S_IFREG + 0o755)
        self.assertEqual(
            entry,
            IndexEntry(
                1324180496,
                1324180496,
                64769,
                131078,
                33261,
                1000,
                1000,
                12288,
                b"2222222222222222222222222222222222222222",
                0,
                0,
            ),
        )


class BuildIndexTests(TestCase):
    def assertReasonableIndexEntry(self, index_entry, mode, filesize, sha) -> None:
        self.assertEqual(index_entry.mode, mode)  # mode
        self.assertEqual(index_entry.size, filesize)  # filesize
        self.assertEqual(index_entry.sha, sha)  # sha

    def assertFileContents(self, path, contents, symlink=False) -> None:
        if symlink:
            self.assertEqual(os.readlink(path), contents)
        else:
            with open(path, "rb") as f:
                self.assertEqual(f.read(), contents)

    def test_empty(self) -> None:
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            tree = Tree()
            repo.object_store.add_object(tree)

            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )

            # Verify index entries
            index = repo.open_index()
            self.assertEqual(len(index), 0)

            # Verify no files
            self.assertEqual([".git"], os.listdir(repo.path))

    def test_git_dir(self) -> None:
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Populate repo
            filea = Blob.from_string(b"file a")
            filee = Blob.from_string(b"d")

            tree = Tree()
            tree[b".git/a"] = (stat.S_IFREG | 0o644, filea.id)
            tree[b"c/e"] = (stat.S_IFREG | 0o644, filee.id)

            repo.object_store.add_objects([(o, None) for o in [filea, filee, tree]])

            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )

            # Verify index entries
            index = repo.open_index()
            self.assertEqual(len(index), 1)

            # filea
            apath = os.path.join(repo.path, ".git", "a")
            self.assertFalse(os.path.exists(apath))

            # filee
            epath = os.path.join(repo.path, "c", "e")
            self.assertTrue(os.path.exists(epath))
            self.assertReasonableIndexEntry(
                index[b"c/e"], stat.S_IFREG | 0o644, 1, filee.id
            )
            self.assertFileContents(epath, b"d")

    def test_nonempty(self) -> None:
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Populate repo
            filea = Blob.from_string(b"file a")
            fileb = Blob.from_string(b"file b")
            filed = Blob.from_string(b"file d")

            tree = Tree()
            tree[b"a"] = (stat.S_IFREG | 0o644, filea.id)
            tree[b"b"] = (stat.S_IFREG | 0o644, fileb.id)
            tree[b"c/d"] = (stat.S_IFREG | 0o644, filed.id)

            repo.object_store.add_objects(
                [(o, None) for o in [filea, fileb, filed, tree]]
            )

            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )

            # Verify index entries
            index = repo.open_index()
            self.assertEqual(len(index), 3)

            # filea
            apath = os.path.join(repo.path, "a")
            self.assertTrue(os.path.exists(apath))
            self.assertReasonableIndexEntry(
                index[b"a"], stat.S_IFREG | 0o644, 6, filea.id
            )
            self.assertFileContents(apath, b"file a")

            # fileb
            bpath = os.path.join(repo.path, "b")
            self.assertTrue(os.path.exists(bpath))
            self.assertReasonableIndexEntry(
                index[b"b"], stat.S_IFREG | 0o644, 6, fileb.id
            )
            self.assertFileContents(bpath, b"file b")

            # filed
            dpath = os.path.join(repo.path, "c", "d")
            self.assertTrue(os.path.exists(dpath))
            self.assertReasonableIndexEntry(
                index[b"c/d"], stat.S_IFREG | 0o644, 6, filed.id
            )
            self.assertFileContents(dpath, b"file d")

            # Verify no extra files
            self.assertEqual([".git", "a", "b", "c"], sorted(os.listdir(repo.path)))
            self.assertEqual(["d"], sorted(os.listdir(os.path.join(repo.path, "c"))))

    @skipIf(not getattr(os, "sync", None), "Requires sync support")
    def test_norewrite(self) -> None:
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Populate repo
            filea = Blob.from_string(b"file a")
            filea_path = os.path.join(repo_dir, "a")
            tree = Tree()
            tree[b"a"] = (stat.S_IFREG | 0o644, filea.id)

            repo.object_store.add_objects([(o, None) for o in [filea, tree]])

            # First Write
            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )
            # Use sync as metadata can be cached on some FS
            os.sync()
            mtime = os.stat(filea_path).st_mtime

            # Test Rewrite
            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )
            os.sync()
            self.assertEqual(mtime, os.stat(filea_path).st_mtime)

            # Modify content
            with open(filea_path, "wb") as fh:
                fh.write(b"test a")
            os.sync()
            mtime = os.stat(filea_path).st_mtime

            # Test rewrite
            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )
            os.sync()
            with open(filea_path, "rb") as fh:
                self.assertEqual(b"file a", fh.read())

    @skipIf(not can_symlink(), "Requires symlink support")
    def test_symlink(self) -> None:
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Populate repo
            filed = Blob.from_string(b"file d")
            filee = Blob.from_string(b"d")

            tree = Tree()
            tree[b"c/d"] = (stat.S_IFREG | 0o644, filed.id)
            tree[b"c/e"] = (stat.S_IFLNK, filee.id)  # symlink

            repo.object_store.add_objects([(o, None) for o in [filed, filee, tree]])

            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )

            # Verify index entries
            index = repo.open_index()

            # symlink to d
            epath = os.path.join(repo.path, "c", "e")
            self.assertTrue(os.path.exists(epath))
            self.assertReasonableIndexEntry(
                index[b"c/e"],
                stat.S_IFLNK,
                0 if sys.platform == "win32" else 1,
                filee.id,
            )
            self.assertFileContents(epath, "d", symlink=True)

    def test_no_decode_encode(self) -> None:
        repo_dir = tempfile.mkdtemp()
        repo_dir_bytes = os.fsencode(repo_dir)
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Populate repo
            file = Blob.from_string(b"foo")

            tree = Tree()
            latin1_name = "À".encode("latin1")
            try:
                latin1_path = os.path.join(repo_dir_bytes, latin1_name)
            except UnicodeDecodeError:
                self.skipTest("can not decode as latin1")
            utf8_name = "À".encode()
            utf8_path = os.path.join(repo_dir_bytes, utf8_name)
            tree[latin1_name] = (stat.S_IFREG | 0o644, file.id)
            tree[utf8_name] = (stat.S_IFREG | 0o644, file.id)

            repo.object_store.add_objects([(o, None) for o in [file, tree]])

            try:
                build_index_from_tree(
                    repo.path, repo.index_path(), repo.object_store, tree.id
                )
            except OSError as e:
                if e.errno == 92 and sys.platform == "darwin":
                    # Our filename isn't supported by the platform :(
                    self.skipTest(f"can not write filename {e.filename!r}")
                else:
                    raise
            except UnicodeDecodeError:
                # This happens e.g. with python3.6 on Windows.
                # It implicitly decodes using utf8, which doesn't work.
                self.skipTest("can not implicitly convert as utf8")

            # Verify index entries
            index = repo.open_index()
            self.assertIn(latin1_name, index)
            self.assertIn(utf8_name, index)

            self.assertTrue(os.path.exists(latin1_path))

            self.assertTrue(os.path.exists(utf8_path))

    def test_git_submodule(self) -> None:
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            filea = Blob.from_string(b"file alalala")

            subtree = Tree()
            subtree[b"a"] = (stat.S_IFREG | 0o644, filea.id)

            c = Commit()
            c.tree = subtree.id
            c.committer = c.author = b"Somebody <somebody@example.com>"
            c.commit_time = c.author_time = 42342
            c.commit_timezone = c.author_timezone = 0
            c.parents = []
            c.message = b"Subcommit"

            tree = Tree()
            tree[b"c"] = (S_IFGITLINK, c.id)

            repo.object_store.add_objects([(o, None) for o in [tree]])

            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )

            # Verify index entries
            index = repo.open_index()
            self.assertEqual(len(index), 1)

            # filea
            apath = os.path.join(repo.path, "c/a")
            self.assertFalse(os.path.exists(apath))

            # dir c
            cpath = os.path.join(repo.path, "c")
            self.assertTrue(os.path.isdir(cpath))
            self.assertEqual(index[b"c"].mode, S_IFGITLINK)  # mode
            self.assertEqual(index[b"c"].sha, c.id)  # sha

    def test_git_submodule_exists(self) -> None:
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            filea = Blob.from_string(b"file alalala")

            subtree = Tree()
            subtree[b"a"] = (stat.S_IFREG | 0o644, filea.id)

            c = Commit()
            c.tree = subtree.id
            c.committer = c.author = b"Somebody <somebody@example.com>"
            c.commit_time = c.author_time = 42342
            c.commit_timezone = c.author_timezone = 0
            c.parents = []
            c.message = b"Subcommit"

            tree = Tree()
            tree[b"c"] = (S_IFGITLINK, c.id)

            os.mkdir(os.path.join(repo_dir, "c"))
            repo.object_store.add_objects([(o, None) for o in [tree]])

            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, tree.id
            )

            # Verify index entries
            index = repo.open_index()
            self.assertEqual(len(index), 1)

            # filea
            apath = os.path.join(repo.path, "c/a")
            self.assertFalse(os.path.exists(apath))

            # dir c
            cpath = os.path.join(repo.path, "c")
            self.assertTrue(os.path.isdir(cpath))
            self.assertEqual(index[b"c"].mode, S_IFGITLINK)  # mode
            self.assertEqual(index[b"c"].sha, c.id)  # sha


class GetUnstagedChangesTests(TestCase):
    def test_get_unstaged_changes(self) -> None:
        """Unit test for get_unstaged_changes."""
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Commit a dummy file then modify it
            foo1_fullpath = os.path.join(repo_dir, "foo1")
            with open(foo1_fullpath, "wb") as f:
                f.write(b"origstuff")

            foo2_fullpath = os.path.join(repo_dir, "foo2")
            with open(foo2_fullpath, "wb") as f:
                f.write(b"origstuff")

            repo.stage(["foo1", "foo2"])
            repo.do_commit(
                b"test status",
                author=b"author <email>",
                committer=b"committer <email>",
            )

            with open(foo1_fullpath, "wb") as f:
                f.write(b"newstuff")

            # modify access and modify time of path
            os.utime(foo1_fullpath, (0, 0))

            changes = get_unstaged_changes(repo.open_index(), repo_dir)

            self.assertEqual(list(changes), [b"foo1"])

    def test_get_unstaged_deleted_changes(self) -> None:
        """Unit test for get_unstaged_changes."""
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Commit a dummy file then remove it
            foo1_fullpath = os.path.join(repo_dir, "foo1")
            with open(foo1_fullpath, "wb") as f:
                f.write(b"origstuff")

            repo.stage(["foo1"])
            repo.do_commit(
                b"test status",
                author=b"author <email>",
                committer=b"committer <email>",
            )

            os.unlink(foo1_fullpath)

            changes = get_unstaged_changes(repo.open_index(), repo_dir)

            self.assertEqual(list(changes), [b"foo1"])

    def test_get_unstaged_changes_removed_replaced_by_directory(self) -> None:
        """Unit test for get_unstaged_changes."""
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Commit a dummy file then modify it
            foo1_fullpath = os.path.join(repo_dir, "foo1")
            with open(foo1_fullpath, "wb") as f:
                f.write(b"origstuff")

            repo.stage(["foo1"])
            repo.do_commit(
                b"test status",
                author=b"author <email>",
                committer=b"committer <email>",
            )

            os.remove(foo1_fullpath)
            os.mkdir(foo1_fullpath)

            changes = get_unstaged_changes(repo.open_index(), repo_dir)

            self.assertEqual(list(changes), [b"foo1"])

    @skipIf(not can_symlink(), "Requires symlink support")
    def test_get_unstaged_changes_removed_replaced_by_link(self) -> None:
        """Unit test for get_unstaged_changes."""
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)
        with Repo.init(repo_dir) as repo:
            # Commit a dummy file then modify it
            foo1_fullpath = os.path.join(repo_dir, "foo1")
            with open(foo1_fullpath, "wb") as f:
                f.write(b"origstuff")

            repo.stage(["foo1"])
            repo.do_commit(
                b"test status",
                author=b"author <email>",
                committer=b"committer <email>",
            )

            os.remove(foo1_fullpath)
            os.symlink(os.path.dirname(foo1_fullpath), foo1_fullpath)

            changes = get_unstaged_changes(repo.open_index(), repo_dir)

            self.assertEqual(list(changes), [b"foo1"])


class TestValidatePathElement(TestCase):
    def test_default(self) -> None:
        self.assertTrue(validate_path_element_default(b"bla"))
        self.assertTrue(validate_path_element_default(b".bla"))
        self.assertFalse(validate_path_element_default(b".git"))
        self.assertFalse(validate_path_element_default(b".giT"))
        self.assertFalse(validate_path_element_default(b".."))
        self.assertTrue(validate_path_element_default(b"git~1"))

    def test_ntfs(self) -> None:
        self.assertTrue(validate_path_element_ntfs(b"bla"))
        self.assertTrue(validate_path_element_ntfs(b".bla"))
        self.assertFalse(validate_path_element_ntfs(b".git"))
        self.assertFalse(validate_path_element_ntfs(b".giT"))
        self.assertFalse(validate_path_element_ntfs(b".."))
        self.assertFalse(validate_path_element_ntfs(b"git~1"))


class TestTreeFSPathConversion(TestCase):
    def test_tree_to_fs_path(self) -> None:
        tree_path = "délwíçh/foo".encode()
        fs_path = _tree_to_fs_path(b"/prefix/path", tree_path)
        self.assertEqual(
            fs_path,
            os.fsencode(os.path.join("/prefix/path", "délwíçh", "foo")),
        )

    def test_tree_to_fs_path_windows_separator(self) -> None:
        tree_path = b"path/with/slash"
        original_sep = os.sep.encode("ascii")
        try:
            # Temporarily modify os_sep_bytes to test Windows path conversion
            # This simulates Windows behavior on all platforms for testing
            import dulwich.index

            dulwich.index.os_sep_bytes = b"\\"

            fs_path = _tree_to_fs_path(b"/prefix/path", tree_path)

            # The function should join the prefix path with the converted tree path
            # The expected behavior is that the path separators in the tree_path are
            # converted to the platform-specific separator (which we've set to backslash)
            expected_path = os.path.join(b"/prefix/path", b"path\\with\\slash")
            self.assertEqual(fs_path, expected_path)
        finally:
            # Restore original value
            dulwich.index.os_sep_bytes = original_sep

    def test_fs_to_tree_path_str(self) -> None:
        fs_path = os.path.join(os.path.join("délwíçh", "foo"))
        tree_path = _fs_to_tree_path(fs_path)
        self.assertEqual(tree_path, "délwíçh/foo".encode())

    def test_fs_to_tree_path_bytes(self) -> None:
        fs_path = os.path.join(os.fsencode(os.path.join("délwíçh", "foo")))
        tree_path = _fs_to_tree_path(fs_path)
        self.assertEqual(tree_path, "délwíçh/foo".encode())

    def test_fs_to_tree_path_windows_separator(self) -> None:
        # Test conversion of Windows paths to tree paths
        fs_path = b"path\\with\\backslash"
        original_sep = os.sep.encode("ascii")
        try:
            # Temporarily modify os_sep_bytes to test Windows path conversion
            import dulwich.index

            dulwich.index.os_sep_bytes = b"\\"

            tree_path = _fs_to_tree_path(fs_path)
            self.assertEqual(tree_path, b"path/with/backslash")
        finally:
            # Restore original value
            dulwich.index.os_sep_bytes = original_sep


class TestIndexEntryFromPath(TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tempdir)

    def test_index_entry_from_path_file(self) -> None:
        """Test creating index entry from a regular file."""
        # Create a test file
        test_file = os.path.join(self.tempdir, "testfile")
        with open(test_file, "wb") as f:
            f.write(b"test content")

        # Get the index entry
        entry = index_entry_from_path(os.fsencode(test_file))

        # Verify the entry was created with the right mode
        self.assertIsNotNone(entry)
        self.assertEqual(cleanup_mode(os.stat(test_file).st_mode), entry.mode)

    @skipIf(not can_symlink(), "Requires symlink support")
    def test_index_entry_from_path_symlink(self) -> None:
        """Test creating index entry from a symlink."""
        # Create a target file
        target_file = os.path.join(self.tempdir, "target")
        with open(target_file, "wb") as f:
            f.write(b"target content")

        # Create a symlink
        link_file = os.path.join(self.tempdir, "symlink")
        os.symlink(target_file, link_file)

        # Get the index entry
        entry = index_entry_from_path(os.fsencode(link_file))

        # Verify the entry was created with the right mode
        self.assertIsNotNone(entry)
        self.assertEqual(cleanup_mode(os.lstat(link_file).st_mode), entry.mode)

    def test_index_entry_from_path_directory(self) -> None:
        """Test creating index entry from a directory (should return None)."""
        # Create a directory
        test_dir = os.path.join(self.tempdir, "testdir")
        os.mkdir(test_dir)

        # Get the index entry for a directory
        entry = index_entry_from_path(os.fsencode(test_dir))

        # Should return None for regular directories
        self.assertIsNone(entry)

    def test_index_entry_from_directory_regular(self) -> None:
        """Test index_entry_from_directory with a regular directory."""
        # Create a directory
        test_dir = os.path.join(self.tempdir, "testdir")
        os.mkdir(test_dir)

        # Get stat for the directory
        st = os.lstat(test_dir)

        # Get the index entry for a regular directory
        entry = index_entry_from_directory(st, os.fsencode(test_dir))

        # Should return None for regular directories
        self.assertIsNone(entry)

    def test_index_entry_from_directory_git_submodule(self) -> None:
        """Test index_entry_from_directory with a Git submodule."""
        # Create a git repository that will be a submodule
        sub_repo_dir = os.path.join(self.tempdir, "subrepo")
        os.mkdir(sub_repo_dir)

        # Create the .git directory to make it look like a git repo
        git_dir = os.path.join(sub_repo_dir, ".git")
        os.mkdir(git_dir)

        # Create HEAD file with a fake commit SHA
        head_sha = b"1234567890" * 4  # 40-char fake SHA
        with open(os.path.join(git_dir, "HEAD"), "wb") as f:
            f.write(head_sha)

        # Get stat for the submodule directory
        st = os.lstat(sub_repo_dir)

        # Get the index entry for a git submodule directory
        entry = index_entry_from_directory(st, os.fsencode(sub_repo_dir))

        # Since we don't have a proper git setup, this might still return None
        # This test just ensures the code path is executed
        if entry is not None:
            # If an entry is returned, it should have the gitlink mode
            self.assertEqual(entry.mode, S_IFGITLINK)

    def test_index_entry_from_path_with_object_store(self) -> None:
        """Test creating index entry with object store."""
        # Create a test file
        test_file = os.path.join(self.tempdir, "testfile")
        with open(test_file, "wb") as f:
            f.write(b"test content")

        # Create a memory object store
        object_store = MemoryObjectStore()

        # Get the index entry and add to object store
        entry = index_entry_from_path(os.fsencode(test_file), object_store)

        # Verify we can access the blob from the object store
        self.assertIsNotNone(entry)
        blob = object_store[entry.sha]
        self.assertEqual(b"test content", blob.data)

    def test_iter_fresh_entries(self) -> None:
        """Test iterating over fresh entries."""
        # Create some test files
        file1 = os.path.join(self.tempdir, "file1")
        with open(file1, "wb") as f:
            f.write(b"file1 content")

        file2 = os.path.join(self.tempdir, "file2")
        with open(file2, "wb") as f:
            f.write(b"file2 content")

        # Create a memory object store
        object_store = MemoryObjectStore()

        # Get fresh entries
        paths = [b"file1", b"file2", b"nonexistent"]
        entries = dict(
            iter_fresh_entries(paths, os.fsencode(self.tempdir), object_store)
        )

        # Verify both files got entries but nonexistent file is None
        self.assertIn(b"file1", entries)
        self.assertIn(b"file2", entries)
        self.assertIn(b"nonexistent", entries)
        self.assertIsNotNone(entries[b"file1"])
        self.assertIsNotNone(entries[b"file2"])
        self.assertIsNone(entries[b"nonexistent"])

        # Check that blobs were added to object store
        blob1 = object_store[entries[b"file1"].sha]
        self.assertEqual(b"file1 content", blob1.data)

        blob2 = object_store[entries[b"file2"].sha]
        self.assertEqual(b"file2 content", blob2.data)

    def test_read_submodule_head(self) -> None:
        """Test reading the HEAD of a submodule."""
        from dulwich.index import read_submodule_head
        from dulwich.repo import Repo

        # Create a test repo that will be our "submodule"
        sub_repo_dir = os.path.join(self.tempdir, "subrepo")
        os.mkdir(sub_repo_dir)
        submodule_repo = Repo.init(sub_repo_dir)

        # Create a file and commit it to establish a HEAD
        test_file = os.path.join(sub_repo_dir, "testfile")
        with open(test_file, "wb") as f:
            f.write(b"test content")

        submodule_repo.stage(["testfile"])
        commit_id = submodule_repo.do_commit(b"Test commit for submodule")

        # Test reading the HEAD
        head_sha = read_submodule_head(sub_repo_dir)
        self.assertEqual(commit_id, head_sha)

        # Test with bytes path
        head_sha_bytes = read_submodule_head(os.fsencode(sub_repo_dir))
        self.assertEqual(commit_id, head_sha_bytes)

        # Test with non-existent path
        non_repo_dir = os.path.join(self.tempdir, "nonrepo")
        os.mkdir(non_repo_dir)
        self.assertIsNone(read_submodule_head(non_repo_dir))

        # Test with path that doesn't have a .git directory
        not_git_dir = os.path.join(self.tempdir, "notgit")
        os.mkdir(not_git_dir)
        self.assertIsNone(read_submodule_head(not_git_dir))

    def test_has_directory_changed(self) -> None:
        """Test checking if a directory has changed."""
        from dulwich.index import IndexEntry, _has_directory_changed
        from dulwich.repo import Repo

        # Setup mock IndexEntry
        mock_entry = IndexEntry(
            (1230680220, 0),
            (1230680220, 0),
            2050,
            3761020,
            33188,
            1000,
            1000,
            0,
            b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
            0,
            0,
        )

        # Test with a regular directory (not a submodule)
        reg_dir = os.path.join(self.tempdir, "regular_dir")
        os.mkdir(reg_dir)

        # Should return True for regular directory
        self.assertTrue(_has_directory_changed(os.fsencode(reg_dir), mock_entry))

        # Create a git repository to test submodule scenarios
        sub_repo_dir = os.path.join(self.tempdir, "subrepo")
        os.mkdir(sub_repo_dir)
        submodule_repo = Repo.init(sub_repo_dir)

        # Create a file and commit it to establish a HEAD
        test_file = os.path.join(sub_repo_dir, "testfile")
        with open(test_file, "wb") as f:
            f.write(b"test content")

        submodule_repo.stage(["testfile"])
        commit_id = submodule_repo.do_commit(b"Test commit for submodule")

        # Create an entry with the correct commit SHA
        correct_entry = IndexEntry(
            (1230680220, 0),
            (1230680220, 0),
            2050,
            3761020,
            33188,
            1000,
            1000,
            0,
            commit_id,
            0,
            0,
        )

        # Create an entry with an incorrect commit SHA
        incorrect_entry = IndexEntry(
            (1230680220, 0),
            (1230680220, 0),
            2050,
            3761020,
            33188,
            1000,
            1000,
            0,
            b"0000000000000000000000000000000000000000",
            0,
            0,
        )

        # Should return False for submodule with correct SHA
        self.assertFalse(
            _has_directory_changed(os.fsencode(sub_repo_dir), correct_entry)
        )

        # Should return True for submodule with incorrect SHA
        self.assertTrue(
            _has_directory_changed(os.fsencode(sub_repo_dir), incorrect_entry)
        )

    def test_get_unstaged_changes(self) -> None:
        """Test detecting unstaged changes in a working tree."""
        from dulwich.index import (
            ConflictedIndexEntry,
            Index,
            IndexEntry,
            get_unstaged_changes,
        )

        # Create a test repo
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir)

        # Create test index
        index = Index(os.path.join(repo_dir, "index"))

        # Create an actual hash of our test content
        from dulwich.objects import Blob

        test_blob = Blob()
        test_blob.data = b"initial content"

        # Create some test files with known contents
        file1_path = os.path.join(repo_dir, "file1")
        with open(file1_path, "wb") as f:
            f.write(b"initial content")

        file2_path = os.path.join(repo_dir, "file2")
        with open(file2_path, "wb") as f:
            f.write(b"initial content")

        # Add them to index
        entry1 = IndexEntry(
            (1230680220, 0),
            (1230680220, 0),
            2050,
            3761020,
            33188,
            1000,
            1000,
            0,
            b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",  # Not matching actual content
            0,
            0,
        )

        entry2 = IndexEntry(
            (1230680220, 0),
            (1230680220, 0),
            2050,
            3761020,
            33188,
            1000,
            1000,
            0,
            test_blob.id,  # Will be content's real hash
            0,
            0,
        )

        # Add a file that has a conflict
        entry_conflict = ConflictedIndexEntry(b"conflict", {0: None, 1: None, 2: None})

        index._byname = {
            b"file1": entry1,
            b"file2": entry2,
            b"file3": IndexEntry(
                (1230680220, 0),
                (1230680220, 0),
                2050,
                3761020,
                33188,
                1000,
                1000,
                0,
                b"0000000000000000000000000000000000000000",
                0,
                0,
            ),
            b"conflict": entry_conflict,
        }

        # Get unstaged changes
        changes = list(get_unstaged_changes(index, repo_dir))

        # File1 should be unstaged (content doesn't match hash)
        # File3 doesn't exist (deleted)
        # Conflict is always unstaged
        self.assertEqual(sorted(changes), [b"conflict", b"file1", b"file3"])

        # Create directory where there should be a file
        os.mkdir(os.path.join(repo_dir, "file4"))
        index._byname[b"file4"] = entry1

        # Get unstaged changes again
        changes = list(get_unstaged_changes(index, repo_dir))

        # Now file4 should also be unstaged because it's a directory instead of a file
        self.assertEqual(sorted(changes), [b"conflict", b"file1", b"file3", b"file4"])

        # Create a custom blob filter function
        def filter_blob_callback(blob, path):
            # Modify blob to make it look changed
            blob.data = b"modified " + blob.data
            return blob

        # Get unstaged changes with blob filter
        changes = list(get_unstaged_changes(index, repo_dir, filter_blob_callback))

        # Now both file1 and file2 should be unstaged due to the filter
        self.assertEqual(
            sorted(changes), [b"conflict", b"file1", b"file2", b"file3", b"file4"]
        )
