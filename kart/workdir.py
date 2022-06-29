import contextlib
import click
import logging
from enum import Enum, auto
import functools
from pathlib import Path
import shutil
import subprocess
import sys

import pygit2
import sqlalchemy as sa
from sqlalchemy import Column, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable


from kart.cli_util import tool_environment
from kart import diff_util
from kart.exceptions import NotFound, NO_WORKING_COPY, translate_subprocess_exit_code
from kart.lfs_util import get_local_path_from_lfs_hash
from kart.key_filters import RepoKeyFilter
from kart.point_cloud.v1 import PointCloudV1
from kart.sqlalchemy.sqlite import sqlite_engine
from kart.sqlalchemy.upsert import Upsert as upsert
from kart.working_copy import WorkingCopyPart


Base = declarative_base()


L = logging.getLogger("kart.workdir")


class FileSystemWorkingCopyStatus(Enum):
    """Different status that a file-system working copy can have."""

    UNCREATED = auto()
    PARTIALLY_CREATED = auto()
    CREATED = auto()


class KartState(Base):
    """kart_state table for the workdir that is maintained in .kart/workdir-state.db"""

    __tablename__ = "kart_state"
    table_name = Column(Text, nullable=False, primary_key=True)
    key = Column(Text, nullable=False, primary_key=True)
    value = Column("value", Text, nullable=False)


class FileSystemWorkingCopy(WorkingCopyPart):
    """
    A working copy on the filesystem - also referred to as the "workdir" for brevity.
    Much like Git's working copy but with some key differences:
    - doesn't have an index for staging
    - but does have an index just for tracking which files are dirty, at .kart/workdir-index
    - the files in the workdir aren't necessarily in the exact same place or same format as the
      files in the ODB, so the easiest way to check which ones are dirty is to compare them to the index:
      comparing them to the ODB would involve re-adapting the ODB format to the workdir format.
    - also has a sqlite DB for tracking kart_state, just as the tabular working copies do.
      This is at .kart/workdir-state.db.
    """

    @property
    def WORKING_COPY_TYPE_NAME(self):
        """Human readable name of this part of the working copy, eg "PostGIS"."""
        return "file-system"

    def __str__(self):
        return "file-system working copy"

    def __init__(self, repo):
        super().__init__()

        self.repo = repo
        self.path = repo.workdir_path

        self.index_path = repo.gitdir_file("workdir-index")
        self.state_path = repo.gitdir_file("workdir-state.db")

        self._required_paths = [self.index_path, self.state_path]

    @classmethod
    def get(
        self,
        repo,
        allow_uncreated=False,
        allow_invalid_state=False,
    ):
        wc = FileSystemWorkingCopy(repo)

        if allow_uncreated and allow_invalid_state:
            return wc

        status = wc.status()
        if not allow_invalid_state:
            wc.check_valid_state(status)

        if not allow_uncreated and status == FileSystemWorkingCopyStatus.UNCREATED:
            wc = None

        return wc

    def status(self):
        existing_files = [f for f in self._required_paths if f.is_file()]
        if not existing_files:
            return FileSystemWorkingCopyStatus.UNCREATED
        if existing_files == self._required_paths:
            return FileSystemWorkingCopyStatus.CREATED
        return FileSystemWorkingCopyStatus.PARTIALLY_CREATED

    def check_valid_state(self, status=None):
        if status is None:
            status = self.status()

        if status == FileSystemWorkingCopyStatus.PARTIALLY_CREATED:
            missing_file = next(
                iter([f for f in self._required_paths if not f.is_file()])
            )
            raise NotFound(
                f"File system working copy is corrupt - {missing_file} is missing",
                NO_WORKING_COPY,
            )

    def create_and_initialise(self):
        index = pygit2.Index(str(self.index_path))
        index._repo = self.repo
        index.write()
        engine = sqlite_engine(self.state_path)
        sm = sessionmaker(bind=engine)
        with sm() as s:
            s.execute(CreateTable(KartState.__table__, if_not_exists=True))

    @contextlib.contextmanager
    def state_session(self):
        """
        Context manager for database sessions, yields a connection object inside a transaction

        Calling again yields the _same_ session, the transaction/etc only happen in the outer one.
        """
        L = logging.getLogger(f"{self.__class__.__qualname__}.state_session")

        if hasattr(self, "_session"):
            # Inner call - reuse existing session.
            L.debug("session: existing...")
            yield self._session
            L.debug("session: existing/done")
            return

        L.debug("session: new...")
        engine = sqlite_engine(self.state_path)
        sm = sessionmaker(bind=engine)
        self._session = sm()
        try:
            # TODO - use tidier syntax for opening transactions from sqlalchemy.
            yield self._session
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        finally:
            self._session.close()
            del self._session
            L.debug("session: new/done")

    def get_kart_state_value(self, table_name, key):
        """Returns the hex tree ID from the state table."""
        kart_state = KartState.__table__
        with self.state_session() as sess:
            return sess.scalar(
                sa.select([kart_state.c.value]).where(
                    sa.and_(
                        kart_state.c.table_name == table_name, kart_state.c.key == key
                    )
                )
            )

    def update_state_table_tree(self, tree):
        """Write the given tree to the state table."""
        tree_id = tree.id.hex if isinstance(tree, pygit2.Tree) else tree
        L.info(f"Tree sha: {tree_id}")
        with self.state_session() as sess:
            r = sess.execute(
                upsert(KartState.__table__),
                {"table_name": "*", "key": "tree", "value": tree_id or ""},
            )
        return r.rowcount

    def is_dirty(self):
        """
        Returns True if there are uncommitted changes in the working copy,
        or False otherwise.
        """
        if self.get_tree_id() is None:
            return False

        datasets = self.repo.datasets(
            self.get_tree_id(), filter_dataset_type="point-cloud"
        )
        workdir_diff_cache = self.workdir_diff_cache()
        for dataset in datasets:
            ds_diff = dataset.diff_to_working_copy(workdir_diff_cache)
            ds_diff.prune()
            if ds_diff:
                return True
        return False

    def _is_head(self, commit_or_tree):
        return commit_or_tree.peel(pygit2.Tree) == self.repo.head_tree

    def fetch_lfs_blobs(self, commit_or_tree):
        if commit_or_tree is None:
            return  # Nothing to do.

        extra_args = []
        if isinstance(commit_or_tree, pygit2.Commit) and not self._is_head(
            commit_or_tree
        ):
            # Generally, `lfs fetch` does exactly what we need or at least the best we can do.
            # The exception is resetting to a commit that is not HEAD - then we can tell lfs to fetch that commit.
            remote_name = self.repo.head_remote_name_or_default
            if remote_name:
                extra_args = [remote_name, commit_or_tree.id.hex]

        click.echo("LFS: ", nl=False)
        self.repo.invoke_git("lfs", "fetch", *extra_args)
        click.echo()  # LFS fetch sometimes leaves the cursor at the start of a line that already has text - scroll past that.

    def reset(
        self,
        commit_or_tree,
        *,
        repo_key_filter=RepoKeyFilter.MATCH_ALL,
        track_changes_as_dirty=False,
        rewrite_full=False,
    ):
        """
        Resets the working copy to the given target-tree (or the tree pointed to by the given target-commit).

        Any existing changes which match the repo_key_filter will be discarded. Existing changes which do not
        match the repo_key_filter will be kept.

        If track_changes_as_dirty=False (the default) the tree ID in the kart_state table gets set to the
        new tree ID and the tracking table is left empty. If it is True, the old tree ID is kept and the
        tracking table is used to record all the changes, so that they can be committed later.

        If rewrite_full is True, then every dataset currently being tracked will be dropped, and all datasets
        present at commit_or_tree will be written from scratch using write_full.
        Since write_full honours the current repo spatial filter, this also ensures that the working copy spatial
        filter is up to date.
        """

        # We fetch the LFS tiles immediately before writing them to the working copy - unlike ODB objects that are already fetched.
        self.fetch_lfs_blobs(commit_or_tree)

        if rewrite_full:
            # These aren't supported when we're doing a full rewrite.
            assert repo_key_filter.match_all and not track_changes_as_dirty

        L = logging.getLogger(f"{self.__class__.__qualname__}.reset")
        if commit_or_tree is not None:
            target_tree = commit_or_tree.peel(pygit2.Tree)
            target_tree_id = target_tree.id.hex
        else:
            target_tree_id = target_tree = None

        # base_tree is the tree the working copy is based on.
        # If the working copy exactly matches base_tree, then it is clean,
        # and the workdir-index will also exactly match the workdir contents.

        base_tree_id = self.get_tree_id()
        base_tree = self.repo[base_tree_id] if base_tree_id else None
        repo_tree_id = self.repo.head_tree.hex if self.repo.head_tree else None

        L.debug(
            "reset(): WorkingCopy base_tree:%s, Repo HEAD has tree:%s. Resetting working copy to tree: %s",
            base_tree_id,
            repo_tree_id,
            target_tree_id,
        )
        L.debug("reset(): track_changes_as_dirty=%s", track_changes_as_dirty)

        base_datasets = self.repo.datasets(
            base_tree,
            repo_key_filter=repo_key_filter,
            filter_dataset_type="point-cloud",
        ).datasets_by_path()
        if base_tree == target_tree:
            target_datasets = base_datasets
        else:
            target_datasets = self.repo.datasets(
                target_tree,
                repo_key_filter=repo_key_filter,
                filter_dataset_type="point-cloud",
            ).datasets_by_path()

        ds_inserts = target_datasets.keys() - base_datasets.keys()
        ds_deletes = base_datasets.keys() - target_datasets.keys()
        ds_updates = base_datasets.keys() & target_datasets.keys()

        if rewrite_full:
            for ds_path in ds_updates:
                ds_inserts.add(ds_path)
                ds_deletes.add(ds_path)
            ds_updates.clear()

        structural_changes = ds_inserts | ds_deletes
        is_new_target_tree = base_tree != target_tree
        self._check_for_unsupported_structural_changes(
            structural_changes,
            is_new_target_tree,
            track_changes_as_dirty,
            repo_key_filter,
        )

        if ds_deletes:
            self.delete_datasets_from_workdir([base_datasets[d] for d in ds_deletes])
        if ds_inserts:
            self.write_full_datasets_to_workdir(
                [target_datasets[d] for d in ds_inserts]
            )

        workdir_diff_cache = self.workdir_diff_cache()
        for ds_path in ds_updates:
            # The diffing code can diff from any arbitrary commit, but not from the working copy -
            # it can only diff *to* the working copy.
            # So, we need to diff from=target to=working copy then take the inverse.
            # TODO: Make this less confusing.
            diff_to_apply = ~diff_util.get_dataset_diff(
                ds_path,
                target_datasets,
                base_datasets,
                include_wc_diff=True,
                workdir_diff_cache=workdir_diff_cache,
                ds_filter=repo_key_filter[ds_path],
            )

            self._update_dataset_in_workdir(
                ds_path,
                diff_to_apply,
                ds_filter=repo_key_filter[ds_path],
                track_changes_as_dirty=track_changes_as_dirty,
            )

        if not track_changes_as_dirty:
            self.update_state_table_tree(target_tree_id)

    def write_full_datasets_to_workdir(self, datasets):
        for dataset in datasets:
            assert isinstance(dataset, PointCloudV1)

            wc_tiles_dir = self.path / dataset.path
            (wc_tiles_dir).mkdir(parents=True, exist_ok=True)

            for tilename, lfs_path in dataset.tilenames_with_lfs_paths():
                if not lfs_path.is_file():
                    click.echo(
                        f"Couldn't find tile {tilename} locally - skipping...", err=True
                    )
                    continue
                shutil.copy(lfs_path, wc_tiles_dir / tilename)

        self._reset_workdir_index_for_datasets(datasets)

    def delete_datasets_from_workdir(self, datasets):
        for dataset in datasets:
            assert isinstance(dataset, PointCloudV1)

            ds_tiles_dir = (self.path / dataset.path).resolve()
            # Sanity check to make sure we're not deleting something we shouldn't.
            assert self.path in ds_tiles_dir.parents
            assert self.repo.workdir_path in ds_tiles_dir.parents
            if ds_tiles_dir.is_dir():
                shutil.rmtree(ds_tiles_dir)

        self._reset_workdir_index_for_datasets(datasets)

    def _update_dataset_in_workdir(
        self, ds_path, diff_to_apply, ds_filter, track_changes_as_dirty
    ):
        ds_tiles_dir = (self.path / ds_path).resolve()
        # Sanity check to make sure we're not messing with files we shouldn't:
        assert self.path in ds_tiles_dir.parents
        assert self.repo.workdir_path in ds_tiles_dir.parents
        assert ds_tiles_dir.is_dir()

        do_update_all = ds_filter.match_all
        reset_index_files = []

        tile_diff = diff_to_apply.get("tile")
        if not tile_diff:
            return
        for tile_delta in tile_diff.values():
            if tile_delta.type == "delete" or tile_delta.is_rename():
                tilename = tile_delta.old_key
                tile_path = ds_tiles_dir / tilename
                if tile_path.is_file():
                    tile_path.unlink()
                if not do_update_all:
                    reset_index_files.append(f"{ds_path}/{tilename}")

            if tile_delta.type in ("update", "insert"):
                tilename = tile_delta.new_key
                lfs_path = get_local_path_from_lfs_hash(
                    self.repo, tile_delta.new_value["oid"]
                )
                if not lfs_path.is_file():
                    click.echo(
                        f"Couldn't find tile {tilename} locally - skipping...", err=True
                    )
                    continue
                shutil.copy(lfs_path, ds_tiles_dir / tilename)
                if not do_update_all:
                    reset_index_files.append(f"{ds_path}/{tilename}")

        if not track_changes_as_dirty:
            if do_update_all:
                self._reset_workdir_index_for_datasets([ds_path])
            else:
                self._reset_workdir_index_for_files(reset_index_files)

    def _reset_workdir_index_for_datasets(
        self, datasets, repo_key_filter=RepoKeyFilter.MATCH_ALL
    ):
        def path(ds_or_path):
            return ds_or_path.path if hasattr(ds_or_path, "path") else ds_or_path

        paths = [path(d) for d in datasets]

        env = tool_environment()
        env["GIT_INDEX_FILE"] = str(self.index_path)

        try:
            # Use Git to figure out which files in the in the index need to be updated.
            cmd = [
                "git",
                "add",
                "--all",
                "--intent-to-add",
                "--dry-run",
                "--",
                *paths,
            ]
            output = subprocess.check_output(
                cmd, env=env, encoding="utf-8", cwd=self.path
            ).strip()
        except subprocess.CalledProcessError as e:
            sys.exit(translate_subprocess_exit_code(e.returncode))

        if not output:
            # Nothing to be done.
            return

        def parse_path(line):
            path = line.strip().split(maxsplit=1)[1]
            if path.startswith("'") or path.startswith('"'):
                path = path[1:-1]
            return path

        def matches_repo_key_filter(path):
            path = Path(path.replace("\\", "/"))
            ds_path = str(path.parents[0])
            tile_name = path.name
            return repo_key_filter.recursive_get([ds_path, "tile", tile_name])

        # Use git update-index to reset these paths - we can't use git add directly since
        # that is for staging files which involves also writing them to the ODB, which we don't want.
        file_paths = [parse_path(line) for line in output.splitlines()]
        if not repo_key_filter.match_all:
            file_paths = [p for p in file_paths if matches_repo_key_filter(p)]
        self._reset_workdir_index_for_files(file_paths)

    def _reset_workdir_index_for_files(self, file_paths):
        """
        Creates a file <GIT-DIR>/workdir-index that is an index of that part of the contents of the workdir
        that is contained within the given update_index_paths (which can be files or folders).
        """
        # NOTE - we could also try to use a pygit2.Index to do this - but however we do this, it is
        # important that we don't store just (path, OID, mode) for each entry, but that we also store
        # the file's `stat` information - this allows for an optimisation where diffs can be generated
        # without hashing the working copy files. pygit2.Index doesn't give easy access to this info.

        env = tool_environment()
        env["GIT_INDEX_FILE"] = str(self.index_path)

        try:
            # --info-only means don't write the file to the ODB - we only want to update the index.
            cmd = ["git", "update-index", "--add", "--remove", "--info-only", "--stdin"]
            subprocess.run(
                cmd,
                check=True,
                env=env,
                cwd=self.path,
                input="\n".join(file_paths),
                encoding="utf-8",
            )
        except subprocess.CalledProcessError as e:
            sys.exit(translate_subprocess_exit_code(e.returncode))

    def soft_reset_after_commit(
        self, commit_or_tree, *, mark_as_clean=None, now_outside_spatial_filter=None
    ):
        # TODO - handle finer-grained soft-resets than entire datasets
        datasets = self.repo.datasets(
            commit_or_tree,
            repo_key_filter=mark_as_clean,
            filter_dataset_type="point-cloud",
        )

        self._reset_workdir_index_for_datasets(datasets, repo_key_filter=mark_as_clean)
        self.update_state_table_tree(commit_or_tree.peel(pygit2.Tree))

    def raw_diff_from_index(self):
        """Uses the index self.index_path to generate a pygit2 Diff of what's changed in the workdir."""
        index = pygit2.Index(str(self.index_path))
        index._repo = self.repo
        return index.diff_to_workdir(
            pygit2.GIT_DIFF_INCLUDE_UNTRACKED
            | pygit2.GIT_DIFF_RECURSE_UNTRACKED_DIRS
            | pygit2.GIT_DIFF_UPDATE_INDEX
            # GIT_DIFF_UPDATE_INDEX just updates timestamps in the index to make the diff quicker next time
            # none of the paths or hashes change, and the end result stays the same.
        )

    def workdir_deltas_by_dataset_path(self, raw_diff_from_index=None):
        """Returns all the deltas from self.raw_diff_from_index() but grouped by dataset path."""
        if raw_diff_from_index is None:
            raw_diff_from_index = self.raw_diff_from_index()

        all_ds_paths = self.repo.datasets(self.get_tree_id()).paths()

        with_and_without_slash = [
            (p.rstrip("/") + "/", p.rstrip("/")) for p in all_ds_paths
        ]

        def find_ds_path(delta):
            path = delta.old_file.path if delta.old_file else delta.new_file.path
            for with_slash, without_slash in with_and_without_slash:
                if path.startswith(with_slash):
                    return without_slash

        deltas_by_ds_path = {}
        for delta in raw_diff_from_index.deltas:
            ds_path = find_ds_path(delta)
            deltas_by_ds_path.setdefault(ds_path, []).append(delta)

        return deltas_by_ds_path

    def workdir_diff_cache(self):
        """
        Returns a WorkdirDiffCache that acts as a caching layer for this working copy -
        the results of certain operations such as raw_diff_from_index can be cached for the
        duration of a diff.
        """
        return WorkdirDiffCache(self)


class WorkdirDiffCache:
    """
    When we do use the index to diff the workdir, we get a diff for the entire workdir.
    The diffing code otherwise performs diffs per-dataset, so we use this class to cache
    the result of that diff so we can reuse it for the next dataset diff.

    - We don't want to run it up front, in case there are no datasets that need this info
    - We want to run it as soon a the first dataset needs this info, then cache the result
    - We want the result to stay cached for the duration of the diff operation, but no longer
      (in eg a long-running test, there might be several diffs run and the workdir might change)
    """

    def __init__(self, delegate):
        self.delegate = delegate

    @functools.lru_cache(maxsize=1)
    def raw_diff_from_index(self):
        return self.delegate.raw_diff_from_index()

    @functools.lru_cache(maxsize=1)
    def workdir_deltas_by_dataset_path(self):
        # Make sure the raw diff gets cached too:
        raw_diff_from_index = self.raw_diff_from_index()
        return self.delegate.workdir_deltas_by_dataset_path(raw_diff_from_index)

    def workdir_deltas_for_dataset(self, dataset):
        if isinstance(dataset, str):
            path = dataset
        else:
            path = dataset.path
        return self.workdir_deltas_by_dataset_path().get(path)