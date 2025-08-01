# repo.py -- For dealing with git repositories.
# Copyright (C) 2007 James Westby <jw+debian@jameswestby.net>
# Copyright (C) 2008-2013 Jelmer Vernooij <jelmer@jelmer.uk>
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


"""Repository access.

This module contains the base class for git repositories
(BaseRepo) and an implementation which uses a repository on
local disk (Repo).

"""

import os
import stat
import sys
import time
import warnings
from collections.abc import Iterable
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Callable,
    Optional,
    Union,
)

if TYPE_CHECKING:
    # There are no circular imports here, but we try to defer imports as long
    # as possible to reduce start-up time for anything that doesn't need
    # these imports.
    from .attrs import GitAttributes
    from .config import ConditionMatcher, ConfigFile, StackedConfig
    from .index import Index
    from .notes import Notes
    from .worktree import WorkTree

from . import replace_me
from .errors import (
    NoIndexPresent,
    NotBlobError,
    NotCommitError,
    NotGitRepository,
    NotTagError,
    NotTreeError,
    RefFormatError,
)
from .file import GitFile
from .hooks import (
    CommitMsgShellHook,
    Hook,
    PostCommitShellHook,
    PostReceiveShellHook,
    PreCommitShellHook,
)
from .object_store import (
    DiskObjectStore,
    MemoryObjectStore,
    MissingObjectFinder,
    ObjectStoreGraphWalker,
    PackBasedObjectStore,
    find_shallow,
    peel_sha,
)
from .objects import (
    Blob,
    Commit,
    ObjectID,
    ShaFile,
    Tag,
    Tree,
    check_hexsha,
    valid_hexsha,
)
from .pack import generate_unpacked_objects
from .refs import (
    ANNOTATED_TAG_SUFFIX,  # noqa: F401
    LOCAL_BRANCH_PREFIX,
    LOCAL_TAG_PREFIX,  # noqa: F401
    SYMREF,  # noqa: F401
    DictRefsContainer,
    DiskRefsContainer,
    InfoRefsContainer,  # noqa: F401
    Ref,
    RefsContainer,
    _set_default_branch,
    _set_head,
    _set_origin_head,
    check_ref_format,  # noqa: F401
    read_packed_refs,  # noqa: F401
    read_packed_refs_with_peeled,  # noqa: F401
    serialize_refs,
    write_packed_refs,  # noqa: F401
)

CONTROLDIR = ".git"
OBJECTDIR = "objects"
REFSDIR = "refs"
REFSDIR_TAGS = "tags"
REFSDIR_HEADS = "heads"
INDEX_FILENAME = "index"
COMMONDIR = "commondir"
GITDIR = "gitdir"
WORKTREES = "worktrees"

BASE_DIRECTORIES = [
    ["branches"],
    [REFSDIR],
    [REFSDIR, REFSDIR_TAGS],
    [REFSDIR, REFSDIR_HEADS],
    ["hooks"],
    ["info"],
]

DEFAULT_BRANCH = b"master"


class InvalidUserIdentity(Exception):
    """User identity is not of the format 'user <email>'."""

    def __init__(self, identity) -> None:
        self.identity = identity


class DefaultIdentityNotFound(Exception):
    """Default identity could not be determined."""


# TODO(jelmer): Cache?
def _get_default_identity() -> tuple[str, str]:
    import socket

    for name in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        username = os.environ.get(name)
        if username:
            break
    else:
        username = None

    try:
        import pwd
    except ImportError:
        fullname = None
    else:
        try:
            entry = pwd.getpwuid(os.getuid())  # type: ignore
        except KeyError:
            fullname = None
        else:
            if getattr(entry, "gecos", None):
                fullname = entry.pw_gecos.split(",")[0]
            else:
                fullname = None
            if username is None:
                username = entry.pw_name
    if not fullname:
        if username is None:
            raise DefaultIdentityNotFound("no username found")
        fullname = username
    email = os.environ.get("EMAIL")
    if email is None:
        if username is None:
            raise DefaultIdentityNotFound("no username found")
        email = f"{username}@{socket.gethostname()}"
    return (fullname, email)


def get_user_identity(config: "StackedConfig", kind: Optional[str] = None) -> bytes:
    """Determine the identity to use for new commits.

    If kind is set, this first checks
    GIT_${KIND}_NAME and GIT_${KIND}_EMAIL.

    If those variables are not set, then it will fall back
    to reading the user.name and user.email settings from
    the specified configuration.

    If that also fails, then it will fall back to using
    the current users' identity as obtained from the host
    system (e.g. the gecos field, $EMAIL, $USER@$(hostname -f).

    Args:
      kind: Optional kind to return identity for,
        usually either "AUTHOR" or "COMMITTER".

    Returns:
      A user identity
    """
    user: Optional[bytes] = None
    email: Optional[bytes] = None
    if kind:
        user_uc = os.environ.get("GIT_" + kind + "_NAME")
        if user_uc is not None:
            user = user_uc.encode("utf-8")
        email_uc = os.environ.get("GIT_" + kind + "_EMAIL")
        if email_uc is not None:
            email = email_uc.encode("utf-8")
    if user is None:
        try:
            user = config.get(("user",), "name")
        except KeyError:
            user = None
    if email is None:
        try:
            email = config.get(("user",), "email")
        except KeyError:
            email = None
    default_user, default_email = _get_default_identity()
    if user is None:
        user = default_user.encode("utf-8")
    if email is None:
        email = default_email.encode("utf-8")
    if email.startswith(b"<") and email.endswith(b">"):
        email = email[1:-1]
    return user + b" <" + email + b">"


def check_user_identity(identity) -> None:
    """Verify that a user identity is formatted correctly.

    Args:
      identity: User identity bytestring
    Raises:
      InvalidUserIdentity: Raised when identity is invalid
    """
    try:
        fst, snd = identity.split(b" <", 1)
    except ValueError as exc:
        raise InvalidUserIdentity(identity) from exc
    if b">" not in snd:
        raise InvalidUserIdentity(identity)
    if b"\0" in identity or b"\n" in identity:
        raise InvalidUserIdentity(identity)


def parse_graftpoints(
    graftpoints: Iterable[bytes],
) -> dict[bytes, list[bytes]]:
    """Convert a list of graftpoints into a dict.

    Args:
      graftpoints: Iterator of graftpoint lines

    Each line is formatted as:
        <commit sha1> <parent sha1> [<parent sha1>]*

    Resulting dictionary is:
        <commit sha1>: [<parent sha1>*]

    https://git.wiki.kernel.org/index.php/GraftPoint
    """
    grafts = {}
    for line in graftpoints:
        raw_graft = line.split(None, 1)

        commit = raw_graft[0]
        if len(raw_graft) == 2:
            parents = raw_graft[1].split()
        else:
            parents = []

        for sha in [commit, *parents]:
            check_hexsha(sha, "Invalid graftpoint")

        grafts[commit] = parents
    return grafts


def serialize_graftpoints(graftpoints: dict[bytes, list[bytes]]) -> bytes:
    """Convert a dictionary of grafts into string.

    The graft dictionary is:
        <commit sha1>: [<parent sha1>*]

    Each line is formatted as:
        <commit sha1> <parent sha1> [<parent sha1>]*

    https://git.wiki.kernel.org/index.php/GraftPoint

    """
    graft_lines = []
    for commit, parents in graftpoints.items():
        if parents:
            graft_lines.append(commit + b" " + b" ".join(parents))
        else:
            graft_lines.append(commit)
    return b"\n".join(graft_lines)


def _set_filesystem_hidden(path) -> None:
    """Mark path as to be hidden if supported by platform and filesystem.

    On win32 uses SetFileAttributesW api:
    <https://docs.microsoft.com/windows/desktop/api/fileapi/nf-fileapi-setfileattributesw>
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes.wintypes import BOOL, DWORD, LPCWSTR

        FILE_ATTRIBUTE_HIDDEN = 2
        SetFileAttributesW = ctypes.WINFUNCTYPE(BOOL, LPCWSTR, DWORD)(
            ("SetFileAttributesW", ctypes.windll.kernel32)
        )

        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not SetFileAttributesW(path, FILE_ATTRIBUTE_HIDDEN):
            pass  # Could raise or log `ctypes.WinError()` here

    # Could implement other platform specific filesystem hiding here


class ParentsProvider:
    def __init__(self, store, grafts={}, shallows=[]) -> None:
        self.store = store
        self.grafts = grafts
        self.shallows = set(shallows)

        # Get commit graph once at initialization for performance
        self.commit_graph = store.get_commit_graph()

    def get_parents(self, commit_id, commit=None):
        try:
            return self.grafts[commit_id]
        except KeyError:
            pass
        if commit_id in self.shallows:
            return []

        # Try to use commit graph for faster parent lookup
        if self.commit_graph:
            parents = self.commit_graph.get_parents(commit_id)
            if parents is not None:
                return parents

        # Fallback to reading the commit object
        if commit is None:
            commit = self.store[commit_id]
        return commit.parents


class BaseRepo:
    """Base class for a git repository.

    This base class is meant to be used for Repository implementations that e.g.
    work on top of a different transport than a standard filesystem path.

    Attributes:
      object_store: Dictionary-like object for accessing
        the objects
      refs: Dictionary-like object with the refs in this
        repository
    """

    def __init__(self, object_store: PackBasedObjectStore, refs: RefsContainer) -> None:
        """Open a repository.

        This shouldn't be called directly, but rather through one of the
        base classes, such as MemoryRepo or Repo.

        Args:
          object_store: Object store to use
          refs: Refs container to use
        """
        self.object_store = object_store
        self.refs = refs

        self._graftpoints: dict[bytes, list[bytes]] = {}
        self.hooks: dict[str, Hook] = {}

    def _determine_file_mode(self) -> bool:
        """Probe the file-system to determine whether permissions can be trusted.

        Returns: True if permissions can be trusted, False otherwise.
        """
        raise NotImplementedError(self._determine_file_mode)

    def _determine_symlinks(self) -> bool:
        """Probe the filesystem to determine whether symlinks can be created.

        Returns: True if symlinks can be created, False otherwise.
        """
        # For now, just mimic the old behaviour
        return sys.platform != "win32"

    def _init_files(
        self, bare: bool, symlinks: Optional[bool] = None, format: Optional[int] = None
    ) -> None:
        """Initialize a default set of named files."""
        from .config import ConfigFile

        self._put_named_file("description", b"Unnamed repository")
        f = BytesIO()
        cf = ConfigFile()
        if format is None:
            format = 0
        if format not in (0, 1):
            raise ValueError(f"Unsupported repository format version: {format}")
        cf.set("core", "repositoryformatversion", str(format))
        if self._determine_file_mode():
            cf.set("core", "filemode", True)
        else:
            cf.set("core", "filemode", False)

        if symlinks is None and not bare:
            symlinks = self._determine_symlinks()

        if symlinks is False:
            cf.set("core", "symlinks", symlinks)

        cf.set("core", "bare", bare)
        cf.set("core", "logallrefupdates", True)
        cf.write_to_file(f)
        self._put_named_file("config", f.getvalue())
        self._put_named_file(os.path.join("info", "exclude"), b"")

    def get_named_file(self, path: str) -> Optional[BinaryIO]:
        """Get a file from the control dir with a specific name.

        Although the filename should be interpreted as a filename relative to
        the control dir in a disk-based Repo, the object returned need not be
        pointing to a file in that location.

        Args:
          path: The path to the file, relative to the control dir.
        Returns: An open file object, or None if the file does not exist.
        """
        raise NotImplementedError(self.get_named_file)

    def _put_named_file(self, path: str, contents: bytes) -> None:
        """Write a file to the control dir with the given name and contents.

        Args:
          path: The path to the file, relative to the control dir.
          contents: A string to write to the file.
        """
        raise NotImplementedError(self._put_named_file)

    def _del_named_file(self, path: str) -> None:
        """Delete a file in the control directory with the given name."""
        raise NotImplementedError(self._del_named_file)

    def open_index(self) -> "Index":
        """Open the index for this repository.

        Raises:
          NoIndexPresent: If no index is present
        Returns: The matching `Index`
        """
        raise NotImplementedError(self.open_index)

    def fetch(
        self, target, determine_wants=None, progress=None, depth: Optional[int] = None
    ):
        """Fetch objects into another repository.

        Args:
          target: The target repository
          determine_wants: Optional function to determine what refs to
            fetch.
          progress: Optional progress function
          depth: Optional shallow fetch depth
        Returns: The local refs
        """
        if determine_wants is None:
            determine_wants = target.object_store.determine_wants_all
        count, pack_data = self.fetch_pack_data(
            determine_wants,
            target.get_graph_walker(),
            progress=progress,
            depth=depth,
        )
        target.object_store.add_pack_data(count, pack_data, progress)
        return self.get_refs()

    def fetch_pack_data(
        self,
        determine_wants,
        graph_walker,
        progress,
        *,
        get_tagged=None,
        depth: Optional[int] = None,
    ):
        """Fetch the pack data required for a set of revisions.

        Args:
          determine_wants: Function that takes a dictionary with heads
            and returns the list of heads to fetch.
          graph_walker: Object that can iterate over the list of revisions
            to fetch and has an "ack" method that will be called to acknowledge
            that a revision is present.
          progress: Simple progress function that will be called with
            updated progress strings.
          get_tagged: Function that returns a dict of pointed-to sha ->
            tag sha for including tags.
          depth: Shallow fetch depth
        Returns: count and iterator over pack data
        """
        missing_objects = self.find_missing_objects(
            determine_wants, graph_walker, progress, get_tagged=get_tagged, depth=depth
        )
        if missing_objects is None:
            return 0, iter([])
        remote_has = missing_objects.get_remote_has()
        object_ids = list(missing_objects)
        return len(object_ids), generate_unpacked_objects(
            self.object_store, object_ids, progress=progress, other_haves=remote_has
        )

    def find_missing_objects(
        self,
        determine_wants,
        graph_walker,
        progress,
        *,
        get_tagged=None,
        depth: Optional[int] = None,
    ) -> Optional[MissingObjectFinder]:
        """Fetch the missing objects required for a set of revisions.

        Args:
          determine_wants: Function that takes a dictionary with heads
            and returns the list of heads to fetch.
          graph_walker: Object that can iterate over the list of revisions
            to fetch and has an "ack" method that will be called to acknowledge
            that a revision is present.
          progress: Simple progress function that will be called with
            updated progress strings.
          get_tagged: Function that returns a dict of pointed-to sha ->
            tag sha for including tags.
          depth: Shallow fetch depth
        Returns: iterator over objects, with __len__ implemented
        """
        refs = serialize_refs(self.object_store, self.get_refs())

        wants = determine_wants(refs)
        if not isinstance(wants, list):
            raise TypeError("determine_wants() did not return a list")

        current_shallow = set(getattr(graph_walker, "shallow", set()))

        if depth not in (None, 0):
            shallow, not_shallow = find_shallow(self.object_store, wants, depth)
            # Only update if graph_walker has shallow attribute
            if hasattr(graph_walker, "shallow"):
                graph_walker.shallow.update(shallow - not_shallow)
                new_shallow = graph_walker.shallow - current_shallow
                unshallow = graph_walker.unshallow = not_shallow & current_shallow
                if hasattr(graph_walker, "update_shallow"):
                    graph_walker.update_shallow(new_shallow, unshallow)
        else:
            unshallow = getattr(graph_walker, "unshallow", frozenset())

        if wants == []:
            # TODO(dborowitz): find a way to short-circuit that doesn't change
            # this interface.

            if getattr(graph_walker, "shallow", set()) or unshallow:
                # Do not send a pack in shallow short-circuit path
                return None

            class DummyMissingObjectFinder:
                def get_remote_has(self) -> None:
                    return None

                def __len__(self) -> int:
                    return 0

                def __iter__(self):
                    yield from []

            return DummyMissingObjectFinder()  # type: ignore

        # If the graph walker is set up with an implementation that can
        # ACK/NAK to the wire, it will write data to the client through
        # this call as a side-effect.
        haves = self.object_store.find_common_revisions(graph_walker)

        # Deal with shallow requests separately because the haves do
        # not reflect what objects are missing
        if getattr(graph_walker, "shallow", set()) or unshallow:
            # TODO: filter the haves commits from iter_shas. the specific
            # commits aren't missing.
            haves = []

        parents_provider = ParentsProvider(self.object_store, shallows=current_shallow)

        def get_parents(commit):
            return parents_provider.get_parents(commit.id, commit)

        return MissingObjectFinder(
            self.object_store,
            haves=haves,
            wants=wants,
            shallow=getattr(graph_walker, "shallow", set()),
            progress=progress,
            get_tagged=get_tagged,
            get_parents=get_parents,
        )

    def generate_pack_data(
        self,
        have: list[ObjectID],
        want: list[ObjectID],
        progress: Optional[Callable[[str], None]] = None,
        ofs_delta: Optional[bool] = None,
    ):
        """Generate pack data objects for a set of wants/haves.

        Args:
          have: List of SHA1s of objects that should not be sent
          want: List of SHA1s of objects that should be sent
          ofs_delta: Whether OFS deltas can be included
          progress: Optional progress reporting method
        """
        return self.object_store.generate_pack_data(
            have,
            want,
            shallow=self.get_shallow(),
            progress=progress,
            ofs_delta=ofs_delta,
        )

    def get_graph_walker(
        self, heads: Optional[list[ObjectID]] = None
    ) -> ObjectStoreGraphWalker:
        """Retrieve a graph walker.

        A graph walker is used by a remote repository (or proxy)
        to find out which objects are present in this repository.

        Args:
          heads: Repository heads to use (optional)
        Returns: A graph walker object
        """
        if heads is None:
            heads = [
                sha
                for sha in self.refs.as_dict(b"refs/heads").values()
                if sha in self.object_store
            ]
        parents_provider = ParentsProvider(self.object_store)
        return ObjectStoreGraphWalker(
            heads,
            parents_provider.get_parents,
            shallow=self.get_shallow(),
            update_shallow=self.update_shallow,
        )

    def get_refs(self) -> dict[bytes, bytes]:
        """Get dictionary with all refs.

        Returns: A ``dict`` mapping ref names to SHA1s
        """
        return self.refs.as_dict()

    def head(self) -> bytes:
        """Return the SHA1 pointed at by HEAD."""
        return self.refs[b"HEAD"]

    def _get_object(self, sha, cls):
        assert len(sha) in (20, 40)
        ret = self.get_object(sha)
        if not isinstance(ret, cls):
            if cls is Commit:
                raise NotCommitError(ret)
            elif cls is Blob:
                raise NotBlobError(ret)
            elif cls is Tree:
                raise NotTreeError(ret)
            elif cls is Tag:
                raise NotTagError(ret)
            else:
                raise Exception(f"Type invalid: {ret.type_name!r} != {cls.type_name!r}")
        return ret

    def get_object(self, sha: bytes) -> ShaFile:
        """Retrieve the object with the specified SHA.

        Args:
          sha: SHA to retrieve
        Returns: A ShaFile object
        Raises:
          KeyError: when the object can not be found
        """
        return self.object_store[sha]

    def parents_provider(self) -> ParentsProvider:
        return ParentsProvider(
            self.object_store,
            grafts=self._graftpoints,
            shallows=self.get_shallow(),
        )

    def get_parents(self, sha: bytes, commit: Optional[Commit] = None) -> list[bytes]:
        """Retrieve the parents of a specific commit.

        If the specific commit is a graftpoint, the graft parents
        will be returned instead.

        Args:
          sha: SHA of the commit for which to retrieve the parents
          commit: Optional commit matching the sha
        Returns: List of parents
        """
        return self.parents_provider().get_parents(sha, commit)

    def get_config(self) -> "ConfigFile":
        """Retrieve the config object.

        Returns: `ConfigFile` object for the ``.git/config`` file.
        """
        raise NotImplementedError(self.get_config)

    def get_worktree_config(self) -> "ConfigFile":
        """Retrieve the worktree config object."""
        raise NotImplementedError(self.get_worktree_config)

    def get_description(self) -> Optional[str]:
        """Retrieve the description for this repository.

        Returns: String with the description of the repository
            as set by the user.
        """
        raise NotImplementedError(self.get_description)

    def set_description(self, description) -> None:
        """Set the description for this repository.

        Args:
          description: Text to set as description for this repository.
        """
        raise NotImplementedError(self.set_description)

    def get_rebase_state_manager(self):
        """Get the appropriate rebase state manager for this repository.

        Returns: RebaseStateManager instance
        """
        raise NotImplementedError(self.get_rebase_state_manager)

    def get_blob_normalizer(self):
        """Return a BlobNormalizer object for checkin/checkout operations.

        Returns: BlobNormalizer instance
        """
        raise NotImplementedError(self.get_blob_normalizer)

    def get_gitattributes(self, tree: Optional[bytes] = None) -> "GitAttributes":
        """Read gitattributes for the repository.

        Args:
            tree: Tree SHA to read .gitattributes from (defaults to HEAD)

        Returns:
            GitAttributes object that can be used to match paths
        """
        raise NotImplementedError(self.get_gitattributes)

    def get_config_stack(self) -> "StackedConfig":
        """Return a config stack for this repository.

        This stack accesses the configuration for both this repository
        itself (.git/config) and the global configuration, which usually
        lives in ~/.gitconfig.

        Returns: `Config` instance for this repository
        """
        from .config import ConfigFile, StackedConfig

        local_config = self.get_config()
        backends: list[ConfigFile] = [local_config]
        if local_config.get_boolean((b"extensions",), b"worktreeconfig", False):
            backends.append(self.get_worktree_config())

        backends += StackedConfig.default_backends()
        return StackedConfig(backends, writable=local_config)

    def get_shallow(self) -> set[ObjectID]:
        """Get the set of shallow commits.

        Returns: Set of shallow commits.
        """
        f = self.get_named_file("shallow")
        if f is None:
            return set()
        with f:
            return {line.strip() for line in f}

    def update_shallow(self, new_shallow, new_unshallow) -> None:
        """Update the list of shallow objects.

        Args:
          new_shallow: Newly shallow objects
          new_unshallow: Newly no longer shallow objects
        """
        shallow = self.get_shallow()
        if new_shallow:
            shallow.update(new_shallow)
        if new_unshallow:
            shallow.difference_update(new_unshallow)
        if shallow:
            self._put_named_file("shallow", b"".join([sha + b"\n" for sha in shallow]))
        else:
            self._del_named_file("shallow")

    def get_peeled(self, ref: Ref) -> ObjectID:
        """Get the peeled value of a ref.

        Args:
          ref: The refname to peel.
        Returns: The fully-peeled SHA1 of a tag object, after peeling all
            intermediate tags; if the original ref does not point to a tag,
            this will equal the original SHA1.
        """
        cached = self.refs.get_peeled(ref)
        if cached is not None:
            return cached
        return peel_sha(self.object_store, self.refs[ref])[1].id

    @property
    def notes(self) -> "Notes":
        """Access notes functionality for this repository.

        Returns:
            Notes object for accessing notes
        """
        from .notes import Notes

        return Notes(self.object_store, self.refs)

    def get_walker(self, include: Optional[list[bytes]] = None, **kwargs):
        """Obtain a walker for this repository.

        Args:
          include: Iterable of SHAs of commits to include along with their
            ancestors. Defaults to [HEAD]

        Keyword Args:
          exclude: Iterable of SHAs of commits to exclude along with their
            ancestors, overriding includes.
          order: ORDER_* constant specifying the order of results.
            Anything other than ORDER_DATE may result in O(n) memory usage.
          reverse: If True, reverse the order of output, requiring O(n)
            memory.
          max_entries: The maximum number of entries to yield, or None for
            no limit.
          paths: Iterable of file or subtree paths to show entries for.
          rename_detector: diff.RenameDetector object for detecting
            renames.
          follow: If True, follow path across renames/copies. Forces a
            default rename_detector.
          since: Timestamp to list commits after.
          until: Timestamp to list commits before.
          queue_cls: A class to use for a queue of commits, supporting the
            iterator protocol. The constructor takes a single argument, the
            Walker.

        Returns: A `Walker` object
        """
        from .walk import Walker

        if include is None:
            include = [self.head()]

        kwargs["get_parents"] = lambda commit: self.get_parents(commit.id, commit)

        return Walker(self.object_store, include, **kwargs)

    def __getitem__(self, name: Union[ObjectID, Ref]):
        """Retrieve a Git object by SHA1 or ref.

        Args:
          name: A Git object SHA1 or a ref name
        Returns: A `ShaFile` object, such as a Commit or Blob
        Raises:
          KeyError: when the specified ref or object does not exist
        """
        if not isinstance(name, bytes):
            raise TypeError(f"'name' must be bytestring, not {type(name).__name__:.80}")
        if len(name) in (20, 40):
            try:
                return self.object_store[name]
            except (KeyError, ValueError):
                pass
        try:
            return self.object_store[self.refs[name]]
        except RefFormatError as exc:
            raise KeyError(name) from exc

    def __contains__(self, name: bytes) -> bool:
        """Check if a specific Git object or ref is present.

        Args:
          name: Git object SHA1 or ref name
        """
        if len(name) == 20 or (len(name) == 40 and valid_hexsha(name)):
            return name in self.object_store or name in self.refs
        else:
            return name in self.refs

    def __setitem__(self, name: bytes, value: Union[ShaFile, bytes]) -> None:
        """Set a ref.

        Args:
          name: ref name
          value: Ref value - either a ShaFile object, or a hex sha
        """
        if name.startswith(b"refs/") or name == b"HEAD":
            if isinstance(value, ShaFile):
                self.refs[name] = value.id
            elif isinstance(value, bytes):
                self.refs[name] = value
            else:
                raise TypeError(value)
        else:
            raise ValueError(name)

    def __delitem__(self, name: bytes) -> None:
        """Remove a ref.

        Args:
          name: Name of the ref to remove
        """
        if name.startswith(b"refs/") or name == b"HEAD":
            del self.refs[name]
        else:
            raise ValueError(name)

    def _get_user_identity(
        self, config: "StackedConfig", kind: Optional[str] = None
    ) -> bytes:
        """Determine the identity to use for new commits."""
        warnings.warn(
            "use get_user_identity() rather than Repo._get_user_identity",
            DeprecationWarning,
        )
        return get_user_identity(config)

    def _add_graftpoints(self, updated_graftpoints: dict[bytes, list[bytes]]) -> None:
        """Add or modify graftpoints.

        Args:
          updated_graftpoints: Dict of commit shas to list of parent shas
        """
        # Simple validation
        for commit, parents in updated_graftpoints.items():
            for sha in [commit, *parents]:
                check_hexsha(sha, "Invalid graftpoint")

        self._graftpoints.update(updated_graftpoints)

    def _remove_graftpoints(self, to_remove: list[bytes] = []) -> None:
        """Remove graftpoints.

        Args:
          to_remove: List of commit shas
        """
        for sha in to_remove:
            del self._graftpoints[sha]

    def _read_heads(self, name):
        f = self.get_named_file(name)
        if f is None:
            return []
        with f:
            return [line.strip() for line in f.readlines() if line.strip()]

    def get_worktree(self) -> "WorkTree":
        """Get the working tree for this repository.

        Returns:
            WorkTree instance for performing working tree operations

        Raises:
            NotImplementedError: If the repository doesn't support working trees
        """
        raise NotImplementedError(
            "Working tree operations not supported by this repository type"
        )

    @replace_me()
    def do_commit(
        self,
        message: Optional[bytes] = None,
        committer: Optional[bytes] = None,
        author: Optional[bytes] = None,
        commit_timestamp=None,
        commit_timezone=None,
        author_timestamp=None,
        author_timezone=None,
        tree: Optional[ObjectID] = None,
        encoding: Optional[bytes] = None,
        ref: Optional[Ref] = b"HEAD",
        merge_heads: Optional[list[ObjectID]] = None,
        no_verify: bool = False,
        sign: bool = False,
    ):
        """Create a new commit.

        If not specified, committer and author default to
        get_user_identity(..., 'COMMITTER')
        and get_user_identity(..., 'AUTHOR') respectively.

        Args:
          message: Commit message (bytes or callable that takes (repo, commit)
            and returns bytes)
          committer: Committer fullname
          author: Author fullname
          commit_timestamp: Commit timestamp (defaults to now)
          commit_timezone: Commit timestamp timezone (defaults to GMT)
          author_timestamp: Author timestamp (defaults to commit
            timestamp)
          author_timezone: Author timestamp timezone
            (defaults to commit timestamp timezone)
          tree: SHA1 of the tree root to use (if not specified the
            current index will be committed).
          encoding: Encoding
          ref: Optional ref to commit to (defaults to current branch).
            If None, creates a dangling commit without updating any ref.
          merge_heads: Merge heads (defaults to .git/MERGE_HEAD)
          no_verify: Skip pre-commit and commit-msg hooks
          sign: GPG Sign the commit (bool, defaults to False,
            pass True to use default GPG key,
            pass a str containing Key ID to use a specific GPG key)

        Returns:
          New commit SHA1
        """
        return self.get_worktree().commit(
            message=message,
            committer=committer,
            author=author,
            commit_timestamp=commit_timestamp,
            commit_timezone=commit_timezone,
            author_timestamp=author_timestamp,
            author_timezone=author_timezone,
            tree=tree,
            encoding=encoding,
            ref=ref,
            merge_heads=merge_heads,
            no_verify=no_verify,
            sign=sign,
        )


def read_gitfile(f):
    """Read a ``.git`` file.

    The first line of the file should start with "gitdir: "

    Args:
      f: File-like object to read from
    Returns: A path
    """
    cs = f.read()
    if not cs.startswith("gitdir: "):
        raise ValueError("Expected file to start with 'gitdir: '")
    return cs[len("gitdir: ") :].rstrip("\n")


class UnsupportedVersion(Exception):
    """Unsupported repository version."""

    def __init__(self, version) -> None:
        self.version = version


class UnsupportedExtension(Exception):
    """Unsupported repository extension."""

    def __init__(self, extension) -> None:
        self.extension = extension


class Repo(BaseRepo):
    """A git repository backed by local disk.

    To open an existing repository, call the constructor with
    the path of the repository.

    To create a new repository, use the Repo.init class method.

    Note that a repository object may hold on to resources such
    as file handles for performance reasons; call .close() to free
    up those resources.

    Attributes:
      path: Path to the working copy (if it exists) or repository control
        directory (if the repository is bare)
      bare: Whether this is a bare repository
    """

    path: str
    bare: bool

    def __init__(
        self,
        root: Union[str, bytes, os.PathLike],
        object_store: Optional[PackBasedObjectStore] = None,
        bare: Optional[bool] = None,
    ) -> None:
        """Open a repository on disk.

        Args:
          root: Path to the repository's root.
          object_store: ObjectStore to use; if omitted, we use the
            repository's default object store
          bare: True if this is a bare repository.
        """
        root = os.fspath(root)
        if isinstance(root, bytes):
            root = os.fsdecode(root)
        hidden_path = os.path.join(root, CONTROLDIR)
        if bare is None:
            if os.path.isfile(hidden_path) or os.path.isdir(
                os.path.join(hidden_path, OBJECTDIR)
            ):
                bare = False
            elif os.path.isdir(os.path.join(root, OBJECTDIR)) and os.path.isdir(
                os.path.join(root, REFSDIR)
            ):
                bare = True
            else:
                raise NotGitRepository(
                    "No git repository was found at {path}".format(**dict(path=root))
                )

        self.bare = bare
        if bare is False:
            if os.path.isfile(hidden_path):
                with open(hidden_path) as f:
                    path = read_gitfile(f)
                self._controldir = os.path.join(root, path)
            else:
                self._controldir = hidden_path
        else:
            self._controldir = root
        commondir = self.get_named_file(COMMONDIR)
        if commondir is not None:
            with commondir:
                self._commondir = os.path.join(
                    self.controldir(),
                    os.fsdecode(commondir.read().rstrip(b"\r\n")),
                )
        else:
            self._commondir = self._controldir
        self.path = root

        # Initialize refs early so they're available for config condition matchers
        self.refs = DiskRefsContainer(
            self.commondir(), self._controldir, logger=self._write_reflog
        )

        # Initialize worktrees container
        from .worktree import WorkTreeContainer

        self.worktrees = WorkTreeContainer(self)

        config = self.get_config()
        try:
            repository_format_version = config.get("core", "repositoryformatversion")
            format_version = (
                0
                if repository_format_version is None
                else int(repository_format_version)
            )
        except KeyError:
            format_version = 0

        if format_version not in (0, 1):
            raise UnsupportedVersion(format_version)

        # Track extensions we encounter
        has_reftable_extension = False
        for extension, value in config.items((b"extensions",)):
            if extension.lower() == b"refstorage":
                if value == b"reftable":
                    has_reftable_extension = True
                else:
                    raise UnsupportedExtension(f"refStorage = {value.decode()}")
            elif extension.lower() not in (b"worktreeconfig",):
                raise UnsupportedExtension(extension)

        if object_store is None:
            object_store = DiskObjectStore.from_config(
                os.path.join(self.commondir(), OBJECTDIR), config
            )

        # Use reftable if extension is configured
        if has_reftable_extension:
            from .reftable import ReftableRefsContainer

            self.refs = ReftableRefsContainer(self.commondir())
            # Update worktrees container after refs change
            self.worktrees = WorkTreeContainer(self)
        BaseRepo.__init__(self, object_store, self.refs)

        self._graftpoints = {}
        graft_file = self.get_named_file(
            os.path.join("info", "grafts"), basedir=self.commondir()
        )
        if graft_file:
            with graft_file:
                self._graftpoints.update(parse_graftpoints(graft_file))
        graft_file = self.get_named_file("shallow", basedir=self.commondir())
        if graft_file:
            with graft_file:
                self._graftpoints.update(parse_graftpoints(graft_file))

        self.hooks["pre-commit"] = PreCommitShellHook(self.path, self.controldir())
        self.hooks["commit-msg"] = CommitMsgShellHook(self.controldir())
        self.hooks["post-commit"] = PostCommitShellHook(self.controldir())
        self.hooks["post-receive"] = PostReceiveShellHook(self.controldir())

    def get_worktree(self) -> "WorkTree":
        """Get the working tree for this repository.

        Returns:
            WorkTree instance for performing working tree operations
        """
        from .worktree import WorkTree

        return WorkTree(self, self.path)

    def _write_reflog(
        self, ref, old_sha, new_sha, committer, timestamp, timezone, message
    ) -> None:
        from .reflog import format_reflog_line

        path = os.path.join(self.controldir(), "logs", os.fsdecode(ref))
        try:
            os.makedirs(os.path.dirname(path))
        except FileExistsError:
            pass
        if committer is None:
            config = self.get_config_stack()
            committer = get_user_identity(config)
        check_user_identity(committer)
        if timestamp is None:
            timestamp = int(time.time())
        if timezone is None:
            timezone = 0  # FIXME
        with open(path, "ab") as f:
            f.write(
                format_reflog_line(
                    old_sha, new_sha, committer, timestamp, timezone, message
                )
                + b"\n"
            )

    def read_reflog(self, ref):
        """Read reflog entries for a reference.

        Args:
          ref: Reference name (e.g. b'HEAD', b'refs/heads/master')

        Yields:
          reflog.Entry objects in chronological order (oldest first)
        """
        from .reflog import read_reflog

        path = os.path.join(self.controldir(), "logs", os.fsdecode(ref))
        try:
            with open(path, "rb") as f:
                yield from read_reflog(f)
        except FileNotFoundError:
            return

    @classmethod
    def discover(cls, start="."):
        """Iterate parent directories to discover a repository.

        Return a Repo object for the first parent directory that looks like a
        Git repository.

        Args:
          start: The directory to start discovery from (defaults to '.')
        """
        remaining = True
        path = os.path.abspath(start)
        while remaining:
            try:
                return cls(path)
            except NotGitRepository:
                path, remaining = os.path.split(path)
        raise NotGitRepository(
            "No git repository was found at {path}".format(**dict(path=start))
        )

    def controldir(self):
        """Return the path of the control directory."""
        return self._controldir

    def commondir(self):
        """Return the path of the common directory.

        For a main working tree, it is identical to controldir().

        For a linked working tree, it is the control directory of the
        main working tree.
        """
        return self._commondir

    def _determine_file_mode(self):
        """Probe the file-system to determine whether permissions can be trusted.

        Returns: True if permissions can be trusted, False otherwise.
        """
        fname = os.path.join(self.path, ".probe-permissions")
        with open(fname, "w") as f:
            f.write("")

        st1 = os.lstat(fname)
        try:
            os.chmod(fname, st1.st_mode ^ stat.S_IXUSR)
        except PermissionError:
            return False
        st2 = os.lstat(fname)

        os.unlink(fname)

        mode_differs = st1.st_mode != st2.st_mode
        st2_has_exec = (st2.st_mode & stat.S_IXUSR) != 0

        return mode_differs and st2_has_exec

    def _determine_symlinks(self):
        """Probe the filesystem to determine whether symlinks can be created.

        Returns: True if symlinks can be created, False otherwise.
        """
        # TODO(jelmer): Actually probe disk / look at filesystem
        return sys.platform != "win32"

    def _put_named_file(self, path, contents) -> None:
        """Write a file to the control dir with the given name and contents.

        Args:
          path: The path to the file, relative to the control dir.
          contents: A string to write to the file.
        """
        path = path.lstrip(os.path.sep)
        with GitFile(os.path.join(self.controldir(), path), "wb") as f:
            f.write(contents)

    def _del_named_file(self, path) -> None:
        try:
            os.unlink(os.path.join(self.controldir(), path))
        except FileNotFoundError:
            return

    def get_named_file(self, path, basedir=None):
        """Get a file from the control dir with a specific name.

        Although the filename should be interpreted as a filename relative to
        the control dir in a disk-based Repo, the object returned need not be
        pointing to a file in that location.

        Args:
          path: The path to the file, relative to the control dir.
          basedir: Optional argument that specifies an alternative to the
            control dir.
        Returns: An open file object, or None if the file does not exist.
        """
        # TODO(dborowitz): sanitize filenames, since this is used directly by
        # the dumb web serving code.
        if basedir is None:
            basedir = self.controldir()
        path = path.lstrip(os.path.sep)
        try:
            return open(os.path.join(basedir, path), "rb")
        except FileNotFoundError:
            return None

    def index_path(self):
        """Return path to the index file."""
        return os.path.join(self.controldir(), INDEX_FILENAME)

    def open_index(self) -> "Index":
        """Open the index for this repository.

        Raises:
          NoIndexPresent: If no index is present
        Returns: The matching `Index`
        """
        from .index import Index

        if not self.has_index():
            raise NoIndexPresent

        # Check for manyFiles feature configuration
        config = self.get_config_stack()
        many_files = config.get_boolean(b"feature", b"manyFiles", False)
        skip_hash = False
        index_version = None

        if many_files:
            # When feature.manyFiles is enabled, set index.version=4 and index.skipHash=true
            try:
                index_version_str = config.get(b"index", b"version")
                index_version = int(index_version_str)
            except KeyError:
                index_version = 4  # Default to version 4 for manyFiles
            skip_hash = config.get_boolean(b"index", b"skipHash", True)
        else:
            # Check for explicit index settings
            try:
                index_version_str = config.get(b"index", b"version")
                index_version = int(index_version_str)
            except KeyError:
                index_version = None
            skip_hash = config.get_boolean(b"index", b"skipHash", False)

        return Index(self.index_path(), skip_hash=skip_hash, version=index_version)

    def has_index(self) -> bool:
        """Check if an index is present."""
        # Bare repos must never have index files; non-bare repos may have a
        # missing index file, which is treated as empty.
        return not self.bare

    @replace_me()
    def stage(
        self,
        fs_paths: Union[
            str, bytes, os.PathLike, Iterable[Union[str, bytes, os.PathLike]]
        ],
    ) -> None:
        """Stage a set of paths.

        Args:
          fs_paths: List of paths, relative to the repository path
        """
        return self.get_worktree().stage(fs_paths)

    @replace_me()
    def unstage(self, fs_paths: list[str]) -> None:
        """Unstage specific file in the index
        Args:
          fs_paths: a list of files to unstage,
            relative to the repository path.
        """
        return self.get_worktree().unstage(fs_paths)

    def clone(
        self,
        target_path,
        *,
        mkdir=True,
        bare=False,
        origin=b"origin",
        checkout=None,
        branch=None,
        progress=None,
        depth: Optional[int] = None,
        symlinks=None,
    ) -> "Repo":
        """Clone this repository.

        Args:
          target_path: Target path
          mkdir: Create the target directory
          bare: Whether to create a bare repository
          checkout: Whether or not to check-out HEAD after cloning
          origin: Base name for refs in target repository
            cloned from this repository
          branch: Optional branch or tag to be used as HEAD in the new repository
            instead of this repository's HEAD.
          progress: Optional progress function
          depth: Depth at which to fetch
          symlinks: Symlinks setting (default to autodetect)
        Returns: Created repository as `Repo`
        """
        encoded_path = os.fsencode(self.path)

        if mkdir:
            os.mkdir(target_path)

        try:
            if not bare:
                target = Repo.init(target_path, symlinks=symlinks)
                if checkout is None:
                    checkout = True
            else:
                if checkout:
                    raise ValueError("checkout and bare are incompatible")
                target = Repo.init_bare(target_path)

            try:
                target_config = target.get_config()
                target_config.set((b"remote", origin), b"url", encoded_path)
                target_config.set(
                    (b"remote", origin),
                    b"fetch",
                    b"+refs/heads/*:refs/remotes/" + origin + b"/*",
                )
                target_config.write_to_path()

                ref_message = b"clone: from " + encoded_path
                self.fetch(target, depth=depth)
                target.refs.import_refs(
                    b"refs/remotes/" + origin,
                    self.refs.as_dict(b"refs/heads"),
                    message=ref_message,
                )
                target.refs.import_refs(
                    b"refs/tags", self.refs.as_dict(b"refs/tags"), message=ref_message
                )

                head_chain, origin_sha = self.refs.follow(b"HEAD")
                origin_head = head_chain[-1] if head_chain else None
                if origin_sha and not origin_head:
                    # set detached HEAD
                    target.refs[b"HEAD"] = origin_sha
                else:
                    _set_origin_head(target.refs, origin, origin_head)
                    head_ref = _set_default_branch(
                        target.refs, origin, origin_head, branch, ref_message
                    )

                    # Update target head
                    if head_ref:
                        head = _set_head(target.refs, head_ref, ref_message)
                    else:
                        head = None

                if checkout and head is not None:
                    target.reset_index()
            except BaseException:
                target.close()
                raise
        except BaseException:
            if mkdir:
                import shutil

                shutil.rmtree(target_path)
            raise
        return target

    @replace_me()
    def reset_index(self, tree: Optional[bytes] = None):
        """Reset the index back to a specific tree.

        Args:
          tree: Tree SHA to reset to, None for current HEAD tree.
        """
        return self.get_worktree().reset_index(tree)

    def _get_config_condition_matchers(self) -> dict[str, "ConditionMatcher"]:
        """Get condition matchers for includeIf conditions.

        Returns a dict of condition prefix to matcher function.
        """
        from pathlib import Path

        from .config import ConditionMatcher, match_glob_pattern

        # Add gitdir matchers
        def match_gitdir(pattern: str, case_sensitive: bool = True) -> bool:
            # Handle relative patterns (starting with ./)
            if pattern.startswith("./"):
                # Can't handle relative patterns without config directory context
                return False

            # Normalize repository path
            try:
                repo_path = str(Path(self._controldir).resolve())
            except (OSError, ValueError):
                return False

            # Expand ~ in pattern and normalize
            pattern = os.path.expanduser(pattern)

            # Normalize pattern following Git's rules
            pattern = pattern.replace("\\", "/")
            if not pattern.startswith(("~/", "./", "/", "**")):
                # Check for Windows absolute path
                if len(pattern) >= 2 and pattern[1] == ":":
                    pass
                else:
                    pattern = "**/" + pattern
            if pattern.endswith("/"):
                pattern = pattern + "**"

            # Use the existing _match_gitdir_pattern function
            from .config import _match_gitdir_pattern

            pattern_bytes = pattern.encode("utf-8", errors="replace")
            repo_path_bytes = repo_path.encode("utf-8", errors="replace")

            return _match_gitdir_pattern(
                repo_path_bytes, pattern_bytes, ignorecase=not case_sensitive
            )

        # Add onbranch matcher
        def match_onbranch(pattern: str) -> bool:
            try:
                # Get the current branch using refs
                ref_chain, _ = self.refs.follow(b"HEAD")
                head_ref = ref_chain[-1]  # Get the final resolved ref
            except KeyError:
                pass
            else:
                if head_ref and head_ref.startswith(b"refs/heads/"):
                    # Extract branch name from ref
                    branch = head_ref[11:].decode("utf-8", errors="replace")
                    return match_glob_pattern(branch, pattern)
            return False

        matchers: dict[str, ConditionMatcher] = {
            "onbranch:": match_onbranch,
            "gitdir:": lambda pattern: match_gitdir(pattern, True),
            "gitdir/i:": lambda pattern: match_gitdir(pattern, False),
        }

        return matchers

    def get_worktree_config(self) -> "ConfigFile":
        from .config import ConfigFile

        path = os.path.join(self.commondir(), "config.worktree")
        try:
            # Pass condition matchers for includeIf evaluation
            condition_matchers = self._get_config_condition_matchers()
            return ConfigFile.from_path(path, condition_matchers=condition_matchers)
        except FileNotFoundError:
            cf = ConfigFile()
            cf.path = path
            return cf

    def get_config(self) -> "ConfigFile":
        """Retrieve the config object.

        Returns: `ConfigFile` object for the ``.git/config`` file.
        """
        from .config import ConfigFile

        path = os.path.join(self._commondir, "config")
        try:
            # Pass condition matchers for includeIf evaluation
            condition_matchers = self._get_config_condition_matchers()
            return ConfigFile.from_path(path, condition_matchers=condition_matchers)
        except FileNotFoundError:
            ret = ConfigFile()
            ret.path = path
            return ret

    def get_rebase_state_manager(self):
        """Get the appropriate rebase state manager for this repository.

        Returns: DiskRebaseStateManager instance
        """
        import os

        from .rebase import DiskRebaseStateManager

        path = os.path.join(self.controldir(), "rebase-merge")
        return DiskRebaseStateManager(path)

    def get_description(self):
        """Retrieve the description of this repository.

        Returns: A string describing the repository or None.
        """
        path = os.path.join(self._controldir, "description")
        try:
            with GitFile(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def __repr__(self) -> str:
        return f"<Repo at {self.path!r}>"

    def set_description(self, description) -> None:
        """Set the description for this repository.

        Args:
          description: Text to set as description for this repository.
        """
        self._put_named_file("description", description)

    @classmethod
    def _init_maybe_bare(
        cls,
        path: Union[str, bytes, os.PathLike],
        controldir: Union[str, bytes, os.PathLike],
        bare,
        object_store=None,
        config=None,
        default_branch=None,
        symlinks: Optional[bool] = None,
        format: Optional[int] = None,
    ):
        path = os.fspath(path)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        controldir = os.fspath(controldir)
        if isinstance(controldir, bytes):
            controldir = os.fsdecode(controldir)
        for d in BASE_DIRECTORIES:
            os.mkdir(os.path.join(controldir, *d))
        if object_store is None:
            object_store = DiskObjectStore.init(os.path.join(controldir, OBJECTDIR))
        ret = cls(path, bare=bare, object_store=object_store)
        if default_branch is None:
            if config is None:
                from .config import StackedConfig

                config = StackedConfig.default()
            try:
                default_branch = config.get("init", "defaultBranch")
            except KeyError:
                default_branch = DEFAULT_BRANCH
        ret.refs.set_symbolic_ref(b"HEAD", LOCAL_BRANCH_PREFIX + default_branch)
        ret._init_files(bare=bare, symlinks=symlinks, format=format)
        return ret

    @classmethod
    def init(
        cls,
        path: Union[str, bytes, os.PathLike],
        *,
        mkdir: bool = False,
        config=None,
        default_branch=None,
        symlinks: Optional[bool] = None,
        format: Optional[int] = None,
    ) -> "Repo":
        """Create a new repository.

        Args:
          path: Path in which to create the repository
          mkdir: Whether to create the directory
          format: Repository format version (defaults to 0)
        Returns: `Repo` instance
        """
        path = os.fspath(path)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if mkdir:
            os.mkdir(path)
        controldir = os.path.join(path, CONTROLDIR)
        os.mkdir(controldir)
        _set_filesystem_hidden(controldir)
        return cls._init_maybe_bare(
            path,
            controldir,
            False,
            config=config,
            default_branch=default_branch,
            symlinks=symlinks,
            format=format,
        )

    @classmethod
    def _init_new_working_directory(
        cls,
        path: Union[str, bytes, os.PathLike],
        main_repo,
        identifier=None,
        mkdir=False,
    ):
        """Create a new working directory linked to a repository.

        Args:
          path: Path in which to create the working tree.
          main_repo: Main repository to reference
          identifier: Worktree identifier
          mkdir: Whether to create the directory
        Returns: `Repo` instance
        """
        path = os.fspath(path)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if mkdir:
            os.mkdir(path)
        if identifier is None:
            identifier = os.path.basename(path)
        # Ensure we use absolute path for the worktree control directory
        main_controldir = os.path.abspath(main_repo.controldir())
        main_worktreesdir = os.path.join(main_controldir, WORKTREES)
        worktree_controldir = os.path.join(main_worktreesdir, identifier)
        gitdirfile = os.path.join(path, CONTROLDIR)
        with open(gitdirfile, "wb") as f:
            f.write(b"gitdir: " + os.fsencode(worktree_controldir) + b"\n")
        try:
            os.mkdir(main_worktreesdir)
        except FileExistsError:
            pass
        try:
            os.mkdir(worktree_controldir)
        except FileExistsError:
            pass
        with open(os.path.join(worktree_controldir, GITDIR), "wb") as f:
            f.write(os.fsencode(gitdirfile) + b"\n")
        with open(os.path.join(worktree_controldir, COMMONDIR), "wb") as f:
            f.write(b"../..\n")
        with open(os.path.join(worktree_controldir, "HEAD"), "wb") as f:
            f.write(main_repo.head() + b"\n")
        r = cls(os.path.normpath(path))
        r.reset_index()
        return r

    @classmethod
    def init_bare(
        cls,
        path: Union[str, bytes, os.PathLike],
        *,
        mkdir=False,
        object_store=None,
        config=None,
        default_branch=None,
        format: Optional[int] = None,
    ):
        """Create a new bare repository.

        ``path`` should already exist and be an empty directory.

        Args:
          path: Path to create bare repository in
          format: Repository format version (defaults to 0)
        Returns: a `Repo` instance
        """
        path = os.fspath(path)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if mkdir:
            os.mkdir(path)
        return cls._init_maybe_bare(
            path,
            path,
            True,
            object_store=object_store,
            config=config,
            default_branch=default_branch,
            format=format,
        )

    create = init_bare

    def close(self) -> None:
        """Close any files opened by this repository."""
        self.object_store.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _read_gitattributes(self) -> dict[bytes, dict[bytes, bytes]]:
        """Read .gitattributes file from working tree.

        Returns:
            Dictionary mapping file patterns to attributes
        """
        gitattributes = {}
        gitattributes_path = os.path.join(self.path, ".gitattributes")

        if os.path.exists(gitattributes_path):
            with open(gitattributes_path, "rb") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith(b"#"):
                        continue

                    parts = line.split()
                    if len(parts) < 2:
                        continue

                    pattern = parts[0]
                    attrs = {}

                    for attr in parts[1:]:
                        if attr.startswith(b"-"):
                            # Unset attribute
                            attrs[attr[1:]] = b"false"
                        elif b"=" in attr:
                            # Set to value
                            key, value = attr.split(b"=", 1)
                            attrs[key] = value
                        else:
                            # Set attribute
                            attrs[attr] = b"true"

                    gitattributes[pattern] = attrs

        return gitattributes

    def get_blob_normalizer(self):
        """Return a BlobNormalizer object."""
        from .filters import FilterBlobNormalizer, FilterRegistry

        # Get proper GitAttributes object
        git_attributes = self.get_gitattributes()
        config_stack = self.get_config_stack()

        # Create FilterRegistry with repo reference
        filter_registry = FilterRegistry(config_stack, self)

        # Return FilterBlobNormalizer which handles all filters including line endings
        return FilterBlobNormalizer(config_stack, git_attributes, filter_registry, self)

    def get_gitattributes(self, tree: Optional[bytes] = None) -> "GitAttributes":
        """Read gitattributes for the repository.

        Args:
            tree: Tree SHA to read .gitattributes from (defaults to HEAD)

        Returns:
            GitAttributes object that can be used to match paths
        """
        from .attrs import (
            GitAttributes,
            Pattern,
            parse_git_attributes,
        )

        patterns = []

        # Read system gitattributes (TODO: implement this)
        # Read global gitattributes (TODO: implement this)

        # Read repository .gitattributes from index/tree
        if tree is None:
            try:
                # Try to get from HEAD
                head = self[b"HEAD"]
                if isinstance(head, Tag):
                    _cls, obj = head.object
                    head = self.get_object(obj)
                tree = head.tree
            except KeyError:
                # No HEAD, no attributes from tree
                pass

        if tree is not None:
            try:
                tree_obj = self[tree]
                if b".gitattributes" in tree_obj:
                    _, attrs_sha = tree_obj[b".gitattributes"]
                    attrs_blob = self[attrs_sha]
                    if isinstance(attrs_blob, Blob):
                        attrs_data = BytesIO(attrs_blob.data)
                        for pattern_bytes, attrs in parse_git_attributes(attrs_data):
                            pattern = Pattern(pattern_bytes)
                            patterns.append((pattern, attrs))
            except (KeyError, NotTreeError):
                pass

        # Read .git/info/attributes
        info_attrs_path = os.path.join(self.controldir(), "info", "attributes")
        if os.path.exists(info_attrs_path):
            with open(info_attrs_path, "rb") as f:
                for pattern_bytes, attrs in parse_git_attributes(f):
                    pattern = Pattern(pattern_bytes)
                    patterns.append((pattern, attrs))

        # Read .gitattributes from working directory (if it exists)
        working_attrs_path = os.path.join(self.path, ".gitattributes")
        if os.path.exists(working_attrs_path):
            with open(working_attrs_path, "rb") as f:
                for pattern_bytes, attrs in parse_git_attributes(f):
                    pattern = Pattern(pattern_bytes)
                    patterns.append((pattern, attrs))

        return GitAttributes(patterns)

    @replace_me()
    def _sparse_checkout_file_path(self) -> str:
        """Return the path of the sparse-checkout file in this repo's control dir."""
        return self.get_worktree()._sparse_checkout_file_path()

    @replace_me()
    def configure_for_cone_mode(self) -> None:
        """Ensure the repository is configured for cone-mode sparse-checkout."""
        return self.get_worktree().configure_for_cone_mode()

    @replace_me()
    def infer_cone_mode(self) -> bool:
        """Return True if 'core.sparseCheckoutCone' is set to 'true' in config, else False."""
        return self.get_worktree().infer_cone_mode()

    @replace_me()
    def get_sparse_checkout_patterns(self) -> list[str]:
        """Return a list of sparse-checkout patterns from info/sparse-checkout.

        Returns:
            A list of patterns. Returns an empty list if the file is missing.
        """
        return self.get_worktree().get_sparse_checkout_patterns()

    @replace_me()
    def set_sparse_checkout_patterns(self, patterns: list[str]) -> None:
        """Write the given sparse-checkout patterns into info/sparse-checkout.

        Creates the info/ directory if it does not exist.

        Args:
            patterns: A list of gitignore-style patterns to store.
        """
        return self.get_worktree().set_sparse_checkout_patterns(patterns)

    @replace_me()
    def set_cone_mode_patterns(self, dirs: Union[list[str], None] = None) -> None:
        """Write the given cone-mode directory patterns into info/sparse-checkout.

        For each directory to include, add an inclusion line that "undoes" the prior
        ``!/*/`` 'exclude' that re-includes that directory and everything under it.
        Never add the same line twice.
        """
        return self.get_worktree().set_cone_mode_patterns(dirs)


class MemoryRepo(BaseRepo):
    """Repo that stores refs, objects, and named files in memory.

    MemoryRepos are always bare: they have no working tree and no index, since
    those have a stronger dependency on the filesystem.
    """

    def __init__(self) -> None:
        """Create a new repository in memory."""
        from .config import ConfigFile

        self._reflog: list[Any] = []
        refs_container = DictRefsContainer({}, logger=self._append_reflog)
        BaseRepo.__init__(self, MemoryObjectStore(), refs_container)  # type: ignore
        self._named_files: dict[str, bytes] = {}
        self.bare = True
        self._config = ConfigFile()
        self._description = None

    def _append_reflog(self, *args) -> None:
        self._reflog.append(args)

    def set_description(self, description) -> None:
        self._description = description

    def get_description(self):
        return self._description

    def _determine_file_mode(self):
        """Probe the file-system to determine whether permissions can be trusted.

        Returns: True if permissions can be trusted, False otherwise.
        """
        return sys.platform != "win32"

    def _determine_symlinks(self):
        """Probe the file-system to determine whether permissions can be trusted.

        Returns: True if permissions can be trusted, False otherwise.
        """
        return sys.platform != "win32"

    def _put_named_file(self, path, contents) -> None:
        """Write a file to the control dir with the given name and contents.

        Args:
          path: The path to the file, relative to the control dir.
          contents: A string to write to the file.
        """
        self._named_files[path] = contents

    def _del_named_file(self, path) -> None:
        try:
            del self._named_files[path]
        except KeyError:
            pass

    def get_named_file(self, path, basedir=None):
        """Get a file from the control dir with a specific name.

        Although the filename should be interpreted as a filename relative to
        the control dir in a disk-baked Repo, the object returned need not be
        pointing to a file in that location.

        Args:
          path: The path to the file, relative to the control dir.
        Returns: An open file object, or None if the file does not exist.
        """
        contents = self._named_files.get(path, None)
        if contents is None:
            return None
        return BytesIO(contents)

    def open_index(self) -> "Index":
        """Fail to open index for this repo, since it is bare.

        Raises:
          NoIndexPresent: Raised when no index is present
        """
        raise NoIndexPresent

    def get_config(self):
        """Retrieve the config object.

        Returns: `ConfigFile` object.
        """
        return self._config

    def get_rebase_state_manager(self):
        """Get the appropriate rebase state manager for this repository.

        Returns: MemoryRebaseStateManager instance
        """
        from .rebase import MemoryRebaseStateManager

        return MemoryRebaseStateManager(self)

    def get_blob_normalizer(self):
        """Return a BlobNormalizer object for checkin/checkout operations."""
        from .filters import FilterBlobNormalizer, FilterRegistry

        # Get GitAttributes object
        git_attributes = self.get_gitattributes()
        config_stack = self.get_config_stack()

        # Create FilterRegistry with repo reference
        filter_registry = FilterRegistry(config_stack, self)

        # Return FilterBlobNormalizer which handles all filters
        return FilterBlobNormalizer(config_stack, git_attributes, filter_registry, self)

    def get_gitattributes(self, tree: Optional[bytes] = None) -> "GitAttributes":
        """Read gitattributes for the repository."""
        from .attrs import GitAttributes

        # Memory repos don't have working trees or gitattributes files
        # Return empty GitAttributes
        return GitAttributes([])

    def do_commit(
        self,
        message: Optional[bytes] = None,
        committer: Optional[bytes] = None,
        author: Optional[bytes] = None,
        commit_timestamp=None,
        commit_timezone=None,
        author_timestamp=None,
        author_timezone=None,
        tree: Optional[ObjectID] = None,
        encoding: Optional[bytes] = None,
        ref: Optional[Ref] = b"HEAD",
        merge_heads: Optional[list[ObjectID]] = None,
        no_verify: bool = False,
        sign: bool = False,
    ):
        """Create a new commit.

        This is a simplified implementation for in-memory repositories that
        doesn't support worktree operations or hooks.

        Args:
          message: Commit message
          committer: Committer fullname
          author: Author fullname
          commit_timestamp: Commit timestamp (defaults to now)
          commit_timezone: Commit timestamp timezone (defaults to GMT)
          author_timestamp: Author timestamp (defaults to commit timestamp)
          author_timezone: Author timestamp timezone (defaults to commit timezone)
          tree: SHA1 of the tree root to use
          encoding: Encoding
          ref: Optional ref to commit to (defaults to current branch).
            If None, creates a dangling commit without updating any ref.
          merge_heads: Merge heads
          no_verify: Skip pre-commit and commit-msg hooks (ignored for MemoryRepo)
          sign: GPG Sign the commit (ignored for MemoryRepo)

        Returns:
          New commit SHA1
        """
        import time

        from .objects import Commit

        if tree is None:
            raise ValueError("tree must be specified for MemoryRepo")

        c = Commit()
        if len(tree) != 40:
            raise ValueError("tree must be a 40-byte hex sha string")
        c.tree = tree

        config = self.get_config_stack()
        if merge_heads is None:
            merge_heads = []
        if committer is None:
            committer = get_user_identity(config, kind="COMMITTER")
        check_user_identity(committer)
        c.committer = committer
        if commit_timestamp is None:
            commit_timestamp = time.time()
        c.commit_time = int(commit_timestamp)
        if commit_timezone is None:
            commit_timezone = 0
        c.commit_timezone = commit_timezone
        if author is None:
            author = get_user_identity(config, kind="AUTHOR")
        c.author = author
        check_user_identity(author)
        if author_timestamp is None:
            author_timestamp = commit_timestamp
        c.author_time = int(author_timestamp)
        if author_timezone is None:
            author_timezone = commit_timezone
        c.author_timezone = author_timezone
        if encoding is None:
            try:
                encoding = config.get(("i18n",), "commitEncoding")
            except KeyError:
                pass
        if encoding is not None:
            c.encoding = encoding

        # Handle message (for MemoryRepo, we don't support callable messages)
        if callable(message):
            message = message(self, c)
            if message is None:
                raise ValueError("Message callback returned None")

        if message is None:
            raise ValueError("No commit message specified")

        c.message = message

        if ref is None:
            # Create a dangling commit
            c.parents = merge_heads
            self.object_store.add_object(c)
        else:
            try:
                old_head = self.refs[ref]
                c.parents = [old_head, *merge_heads]
                self.object_store.add_object(c)
                ok = self.refs.set_if_equals(
                    ref,
                    old_head,
                    c.id,
                    message=b"commit: " + message,
                    committer=committer,
                    timestamp=commit_timestamp,
                    timezone=commit_timezone,
                )
            except KeyError:
                c.parents = merge_heads
                self.object_store.add_object(c)
                ok = self.refs.add_if_new(
                    ref,
                    c.id,
                    message=b"commit: " + message,
                    committer=committer,
                    timestamp=commit_timestamp,
                    timezone=commit_timezone,
                )
            if not ok:
                from .errors import CommitError

                raise CommitError(f"{ref!r} changed during commit")

        return c.id

    @classmethod
    def init_bare(cls, objects, refs, format: Optional[int] = None):
        """Create a new bare repository in memory.

        Args:
          objects: Objects for the new repository,
            as iterable
          refs: Refs as dictionary, mapping names
            to object SHA1s
          format: Repository format version (defaults to 0)
        """
        ret = cls()
        for obj in objects:
            ret.object_store.add_object(obj)
        for refname, sha in refs.items():
            ret.refs.add_if_new(refname, sha)
        ret._init_files(bare=True, format=format)
        return ret
