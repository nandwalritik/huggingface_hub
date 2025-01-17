import os
import shutil
import sys
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Generator
from unittest.mock import Mock

import pytest

from _pytest.fixtures import SubRequest
from huggingface_hub._snapshot_download import snapshot_download
from huggingface_hub.commands.cache import ScanCacheCommand
from huggingface_hub.utils import DeleteCacheStrategy, HFCacheInfo, scan_cache_dir
from huggingface_hub.utils._cache_manager import _try_delete_path

from .testing_constants import TOKEN


VALID_MODEL_ID = "valid_org/test_scan_repo_a"
VALID_DATASET_ID = "valid_org/test_scan_dataset_b"

REPO_A_MAIN_HASH = "401874e6a9c254a8baae85edd8a073921ecbd7f5"
REPO_A_PR_1_HASH = "fc674b0d440d3ea6f94bc4012e33ebd1dfc11b5b"
REPO_A_OTHER_HASH = "1da18ebd9185d146bcf84e308de53715d97d67d1"
REPO_A_MAIN_README_BLOB_HASH = "4baf04727c45b660add228b2934001991bd34b29"


@pytest.fixture
def fx_cache_dir(request: SubRequest) -> Generator[None, None, None]:
    """Add a `cache_dir` attribute pointing to a temporary directory."""
    with TemporaryDirectory() as cache_dir:
        request.cls.cache_dir = Path(cache_dir).resolve()
        yield


@pytest.mark.usefixtures("fx_cache_dir")
class TestMissingCacheUtils(unittest.TestCase):
    cache_dir: Path

    def test_cache_dir_is_missing(self) -> None:
        """Directory to scan does not exist raises ValueError."""
        self.assertRaises(ValueError, scan_cache_dir, self.cache_dir / "does_not_exist")

    def test_cache_dir_is_a_file(self) -> None:
        """Directory to scan is a file raises ValueError."""
        file_path = self.cache_dir / "file.txt"
        file_path.touch()
        self.assertRaises(ValueError, scan_cache_dir, file_path)


@pytest.mark.usefixtures("fx_cache_dir")
class TestValidCacheUtils(unittest.TestCase):
    cache_dir: Path

    def setUp(self) -> None:
        """Setup a clean cache for tests that will remain valid in all tests."""
        # Download latest main
        snapshot_download(
            repo_id=VALID_MODEL_ID,
            repo_type="model",
            cache_dir=self.cache_dir,
            use_auth_token=TOKEN,
        )

        # Download latest commit which is same as `main`
        snapshot_download(
            repo_id=VALID_MODEL_ID,
            revision=REPO_A_MAIN_HASH,
            repo_type="model",
            cache_dir=self.cache_dir,
            use_auth_token=TOKEN,
        )

        # Download the first commit
        snapshot_download(
            repo_id=VALID_MODEL_ID,
            revision=REPO_A_OTHER_HASH,
            repo_type="model",
            cache_dir=self.cache_dir,
            use_auth_token=TOKEN,
        )

        # Download from a PR
        snapshot_download(
            repo_id=VALID_MODEL_ID,
            revision="refs/pr/1",
            repo_type="model",
            cache_dir=self.cache_dir,
            use_auth_token=TOKEN,
        )

        # Download a Dataset repo from "main"
        snapshot_download(
            repo_id=VALID_DATASET_ID,
            revision="main",
            repo_type="dataset",
            cache_dir=self.cache_dir,
            use_auth_token=TOKEN,
        )

    def test_scan_cache_on_valid_cache(self) -> None:
        """Scan the cache dir without warnings."""
        report = scan_cache_dir(self.cache_dir)

        # Check general information about downloaded snapshots
        self.assertEquals(report.size_on_disk, 3547)
        self.assertEquals(len(report.repos), 2)  # Model and dataset
        self.assertEquals(len(report.warnings), 0)  # Repos are valid

        repo_a = [repo for repo in report.repos if repo.repo_id == VALID_MODEL_ID][0]

        # Check repo A general information
        repo_a_path = self.cache_dir / "models--valid_org--test_scan_repo_a"
        self.assertEquals(repo_a.repo_id, VALID_MODEL_ID)
        self.assertEquals(repo_a.repo_type, "model")
        self.assertEquals(repo_a.repo_path, repo_a_path)

        # 4 downloads but 3 revisions because "main" and REPO_A_MAIN_HASH are the same
        self.assertEquals(len(repo_a.revisions), 3)
        self.assertEquals(
            {rev.commit_hash for rev in repo_a.revisions},
            {REPO_A_MAIN_HASH, REPO_A_PR_1_HASH, REPO_A_OTHER_HASH},
        )

        # Repo size on disk is less than sum of revisions !
        self.assertEquals(repo_a.size_on_disk, 1391)
        self.assertEquals(sum(rev.size_on_disk for rev in repo_a.revisions), 4102)

        # Repo nb files is less than sum of revisions !
        self.assertEquals(repo_a.nb_files, 4)
        self.assertEquals(sum(rev.nb_files for rev in repo_a.revisions), 8)

        # 2 REFS in the repo: "main" and "refs/pr/1"
        # We could have add a tag as well
        self.assertEquals(set(repo_a.refs.keys()), {"main", "refs/pr/1"})
        self.assertEquals(repo_a.refs["main"].commit_hash, REPO_A_MAIN_HASH)
        self.assertEquals(repo_a.refs["refs/pr/1"].commit_hash, REPO_A_PR_1_HASH)

        # Check "main" revision information
        main_revision = repo_a.refs["main"]
        main_revision_path = repo_a_path / "snapshots" / REPO_A_MAIN_HASH

        self.assertEquals(main_revision.commit_hash, REPO_A_MAIN_HASH)
        self.assertEquals(main_revision.snapshot_path, main_revision_path)
        self.assertEquals(main_revision.refs, {"main"})

        # Same nb of files and size on disk that the sum
        self.assertEquals(main_revision.nb_files, len(main_revision.files))
        self.assertEquals(
            main_revision.size_on_disk,
            sum(file.size_on_disk for file in main_revision.files),
        )

        # Check readme file from "main" revision
        main_readme_file = [
            file for file in main_revision.files if file.file_name == "README.md"
        ][0]
        main_readme_file_path = main_revision_path / "README.md"
        main_readme_blob_path = repo_a_path / "blobs" / REPO_A_MAIN_README_BLOB_HASH

        self.assertEquals(main_readme_file.file_name, "README.md")
        self.assertEquals(main_readme_file.file_path, main_readme_file_path)
        self.assertEquals(main_readme_file.blob_path, main_readme_blob_path)

        # Check readme file from "refs/pr/1" revision
        pr_1_revision = repo_a.refs["refs/pr/1"]
        pr_1_revision_path = repo_a_path / "snapshots" / REPO_A_PR_1_HASH
        pr_1_readme_file = [
            file for file in pr_1_revision.files if file.file_name == "README.md"
        ][0]
        pr_1_readme_file_path = pr_1_revision_path / "README.md"

        # file_path in "refs/pr/1" revision is different than "main" but same blob path
        self.assertEquals(
            pr_1_readme_file.file_path, pr_1_readme_file_path
        )  # different
        self.assertEquals(pr_1_readme_file.blob_path, main_readme_blob_path)  # same

    def test_cli_scan_cache_quiet(self) -> None:
        """Test output from CLI scan cache with non verbose output.

        End-to-end test just to see if output is in expected format.
        """
        output = StringIO()
        args = Mock()
        args.verbose = 0
        args.dir = self.cache_dir

        # Taken from https://stackoverflow.com/a/34738440
        previous_output = sys.stdout
        sys.stdout = output
        ScanCacheCommand(args).run()
        sys.stdout = previous_output

        expected_output = f"""
        REPO ID                       REPO TYPE SIZE ON DISK NB FILES REFS            LOCAL PATH
        ----------------------------- --------- ------------ -------- --------------- -------------------------------------------------------------------------------------------------------------
        valid_org/test_scan_dataset_b dataset           2.2K        2 main            {self.cache_dir}/datasets--valid_org--test_scan_dataset_b
        valid_org/test_scan_repo_a    model             1.4K        4 main, refs/pr/1 {self.cache_dir}/models--valid_org--test_scan_repo_a

        Done in 0.0s. Scanned 2 repo(s) for a total of \x1b[1m\x1b[31m3.5K\x1b[0m.
        """

        self.assertListEqual(
            output.getvalue().replace("-", "").split(),
            expected_output.replace("-", "").split(),
        )

    def test_cli_scan_cache_verbose(self) -> None:
        """Test output from CLI scan cache with verbose output.

        End-to-end test just to see if output is in expected format.
        """
        output = StringIO()
        args = Mock()
        args.verbose = 1
        args.dir = self.cache_dir

        # Taken from https://stackoverflow.com/a/34738440
        previous_output = sys.stdout
        sys.stdout = output
        ScanCacheCommand(args).run()
        sys.stdout = previous_output

        expected_output = f"""
        REPO ID                       REPO TYPE REVISION                                 SIZE ON DISK NB FILES REFS      LOCAL PATH
        ----------------------------- --------- ---------------------------------------- ------------ -------- --------- ----------------------------------------------------------------------------------------------------------------------------------------------------------------
        valid_org/test_scan_dataset_b dataset   1ac47c6f707cbc4825c2aa431ad5ab8cf09e60ed         2.2K        2 main      {self.cache_dir}/datasets--valid_org--test_scan_dataset_b/snapshots/1ac47c6f707cbc4825c2aa431ad5ab8cf09e60ed
        valid_org/test_scan_repo_a    model     1da18ebd9185d146bcf84e308de53715d97d67d1         1.3K        1           {self.cache_dir}/models--valid_org--test_scan_repo_a/snapshots/1da18ebd9185d146bcf84e308de53715d97d67d1
        valid_org/test_scan_repo_a    model     401874e6a9c254a8baae85edd8a073921ecbd7f5         1.4K        3 main      {self.cache_dir}/models--valid_org--test_scan_repo_a/snapshots/401874e6a9c254a8baae85edd8a073921ecbd7f5
        valid_org/test_scan_repo_a    model     fc674b0d440d3ea6f94bc4012e33ebd1dfc11b5b         1.4K        4 refs/pr/1 {self.cache_dir}/models--valid_org--test_scan_repo_a/snapshots/fc674b0d440d3ea6f94bc4012e33ebd1dfc11b5b

        Done in 0.0s. Scanned 2 repo(s) for a total of \x1b[1m\x1b[31m3.5K\x1b[0m.
        """

        self.assertListEqual(
            output.getvalue().replace("-", "").split(),
            expected_output.replace("-", "").split(),
        )


@pytest.mark.usefixtures("fx_cache_dir")
class TestCorruptedCacheUtils(unittest.TestCase):
    cache_dir: Path
    repo_path: Path

    def setUp(self) -> None:
        """Setup a clean cache for tests that will get corrupted in tests."""
        # Download latest main
        snapshot_download(
            repo_id=VALID_MODEL_ID,
            repo_type="model",
            cache_dir=self.cache_dir,
            use_auth_token=TOKEN,
        )

        self.repo_path = self.cache_dir / "models--valid_org--test_scan_repo_a"

    def test_repo_path_not_valid_dir(self) -> None:
        """Test if found a not valid path in cache dir."""
        # Case 1: a file
        repo_path = self.cache_dir / "a_file_that_should_not_be_there.txt"
        repo_path.touch()

        report = scan_cache_dir(self.cache_dir)
        self.assertEquals(len(report.repos), 1)  # Scan still worked !

        self.assertEqual(len(report.warnings), 1)
        self.assertEqual(
            str(report.warnings[0]), f"Repo path is not a directory: {repo_path}"
        )

        # Case 2: a folder with wrong naming
        os.remove(repo_path)
        repo_path = self.cache_dir / "a_folder_that_should_not_be_there"
        repo_path.mkdir()

        report = scan_cache_dir(self.cache_dir)
        self.assertEquals(len(report.repos), 1)  # Scan still worked !

        self.assertEqual(len(report.warnings), 1)
        self.assertEqual(
            str(report.warnings[0]),
            f"Repo path is not a valid HuggingFace cache directory: {repo_path}",
        )

        # Case 3: good naming but not a dataset/model/space
        shutil.rmtree(repo_path)
        repo_path = self.cache_dir / "not-models--t5-small"
        repo_path.mkdir()

        report = scan_cache_dir(self.cache_dir)
        self.assertEquals(len(report.repos), 1)  # Scan still worked !

        self.assertEqual(len(report.warnings), 1)
        self.assertEqual(
            str(report.warnings[0]),
            "Repo type must be `dataset`, `model` or `space`, found `not-model`"
            f" ({repo_path}).",
        )

    def test_snapshots_path_not_found(self) -> None:
        """Test if snapshots directory is missing in cached repo."""
        snapshots_path = self.repo_path / "snapshots"
        shutil.rmtree(snapshots_path)

        report = scan_cache_dir(self.cache_dir)
        self.assertEquals(len(report.repos), 0)  # Failed

        self.assertEqual(len(report.warnings), 1)
        self.assertEqual(
            str(report.warnings[0]),
            f"Snapshots dir doesn't exist in cached repo: {snapshots_path}",
        )

    def test_file_in_snapshots_dir(self) -> None:
        """Test if snapshots directory contains a file."""
        wrong_file_path = self.repo_path / "snapshots" / "should_not_be_there"
        wrong_file_path.touch()

        report = scan_cache_dir(self.cache_dir)
        self.assertEquals(len(report.repos), 0)  # Failed

        self.assertEqual(len(report.warnings), 1)
        self.assertEqual(
            str(report.warnings[0]),
            f"Snapshots folder corrupted. Found a file: {wrong_file_path}",
        )

    def test_ref_to_missing_revision(self) -> None:
        """Test if a `refs` points to a missing revision."""
        new_ref = self.repo_path / "refs" / "not_main"
        with new_ref.open("w") as f:
            f.write("revision_hash_that_does_not_exist")

        report = scan_cache_dir(self.cache_dir)
        self.assertEquals(len(report.repos), 0)  # Failed

        self.assertEqual(len(report.warnings), 1)
        self.assertEqual(
            str(report.warnings[0]),
            "Reference(s) refer to missing commit hashes:"
            " {'revision_hash_that_does_not_exist': {'not_main'}} "
            + f"({self.repo_path }).",
        )


class TestDeleteRevisionsDryRun(unittest.TestCase):
    cache_info: Mock  # Mocked HFCacheInfo

    def setUp(self) -> None:
        """Set up fake cache scan report."""
        repo_A_path = Path("repo_A")
        blobs_path = repo_A_path / "blobs"
        snapshots_path = repo_A_path / "snapshots_path"

        # Define blob files
        main_only_file = Mock()
        main_only_file.blob_path = blobs_path / "main_only_hash"
        main_only_file.size_on_disk = 1

        detached_only_file = Mock()
        detached_only_file.blob_path = blobs_path / "detached_only_hash"
        detached_only_file.size_on_disk = 10

        pr_1_only_file = Mock()
        pr_1_only_file.blob_path = blobs_path / "pr_1_only_hash"
        pr_1_only_file.size_on_disk = 100

        detached_and_pr_1_only_file = Mock()
        detached_and_pr_1_only_file.blob_path = (
            blobs_path / "detached_and_pr_1_only_hash"
        )
        detached_and_pr_1_only_file.size_on_disk = 1000

        shared_file = Mock()
        shared_file.blob_path = blobs_path / "shared_file_hash"
        shared_file.size_on_disk = 10000

        # Define revisions
        repo_A_rev_main = Mock()
        repo_A_rev_main.commit_hash = "repo_A_rev_main"
        repo_A_rev_main.snapshot_path = snapshots_path / "repo_A_rev_main"
        repo_A_rev_main.files = {main_only_file, shared_file}
        repo_A_rev_main.refs = {"main"}

        repo_A_rev_detached = Mock()
        repo_A_rev_detached.commit_hash = "repo_A_rev_detached"
        repo_A_rev_detached.snapshot_path = snapshots_path / "repo_A_rev_detached"
        repo_A_rev_detached.files = {
            detached_only_file,
            detached_and_pr_1_only_file,
            shared_file,
        }
        repo_A_rev_detached.refs = {}

        repo_A_rev_pr_1 = Mock()
        repo_A_rev_pr_1.commit_hash = "repo_A_rev_pr_1"
        repo_A_rev_pr_1.snapshot_path = snapshots_path / "repo_A_rev_pr_1"
        repo_A_rev_pr_1.files = {
            pr_1_only_file,
            detached_and_pr_1_only_file,
            shared_file,
        }
        repo_A_rev_pr_1.refs = {"refs/pr/1"}

        # Define repo
        repo_A = Mock()
        repo_A.repo_path = Path("repo_A")
        repo_A.size_on_disk = 4444
        repo_A.revisions = {repo_A_rev_main, repo_A_rev_detached, repo_A_rev_pr_1}

        # Define cache
        cache_info = Mock()
        cache_info.repos = [repo_A]
        self.cache_info = cache_info

    def test_delete_detached_revision(self) -> None:
        strategy = HFCacheInfo.delete_revisions(self.cache_info, "repo_A_rev_detached")
        expected = DeleteCacheStrategy(
            expected_freed_size=10,
            blobs={
                # "shared_file_hash" and "detached_and_pr_1_only_hash" are not deleted
                Path("repo_A/blobs/detached_only_hash"),
            },
            refs=set(),  # No ref deleted since detached
            repos=set(),  # No repo deleted as other revisions exist
            snapshots={Path("repo_A/snapshots_path/repo_A_rev_detached")},
        )
        self.assertEqual(strategy, expected)

    def test_delete_pr_1_revision(self) -> None:
        strategy = HFCacheInfo.delete_revisions(self.cache_info, "repo_A_rev_pr_1")
        expected = DeleteCacheStrategy(
            expected_freed_size=100,
            blobs={
                # "shared_file_hash" and "detached_and_pr_1_only_hash" are not deleted
                Path("repo_A/blobs/pr_1_only_hash")
            },
            refs={Path("repo_A/refs/refs/pr/1")},  # Ref is deleted !
            repos=set(),  # No repo deleted as other revisions exist
            snapshots={Path("repo_A/snapshots_path/repo_A_rev_pr_1")},
        )
        self.assertEqual(strategy, expected)

    def test_delete_pr_1_and_detached(self) -> None:
        strategy = HFCacheInfo.delete_revisions(
            self.cache_info, "repo_A_rev_detached", "repo_A_rev_pr_1"
        )
        expected = DeleteCacheStrategy(
            expected_freed_size=1110,
            blobs={
                Path("repo_A/blobs/detached_only_hash"),
                Path("repo_A/blobs/pr_1_only_hash"),
                # blob shared in both revisions and only those two
                Path("repo_A/blobs/detached_and_pr_1_only_hash"),
            },
            refs={Path("repo_A/refs/refs/pr/1")},
            repos=set(),
            snapshots={
                Path("repo_A/snapshots_path/repo_A_rev_detached"),
                Path("repo_A/snapshots_path/repo_A_rev_pr_1"),
            },
        )
        self.assertEqual(strategy, expected)

    def test_delete_all_revisions(self) -> None:
        strategy = HFCacheInfo.delete_revisions(
            self.cache_info, "repo_A_rev_detached", "repo_A_rev_pr_1", "repo_A_rev_main"
        )
        expected = DeleteCacheStrategy(
            expected_freed_size=4444,
            blobs=set(),
            refs=set(),
            repos={Path("repo_A")},  # No remaining revisions: full repo is deleted
            snapshots=set(),
        )
        self.assertEqual(strategy, expected)

    def test_delete_unknown_revision(self) -> None:
        with self.assertLogs() as captured:
            strategy = HFCacheInfo.delete_revisions(
                self.cache_info, "repo_A_rev_detached", "abcdef123456789"
            )

        # Expected is same strategy as without "abcdef123456789"
        expected = HFCacheInfo.delete_revisions(self.cache_info, "repo_A_rev_detached")
        self.assertEqual(strategy, expected)

        # Expect a warning message
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelname, "WARNING")
        self.assertEqual(
            captured.records[0].message,
            "Revision(s) not found - cannot delete them: abcdef123456789",
        )


@pytest.mark.usefixtures("fx_cache_dir")
class TestDeleteStrategyExecute(unittest.TestCase):
    cache_dir: Path

    def test_execute(self) -> None:
        # Repo folders
        repo_A_path = self.cache_dir / "repo_A"
        repo_A_path.mkdir()
        repo_B_path = self.cache_dir / "repo_B"
        repo_B_path.mkdir()

        # Refs files in repo_B
        refs_main_path = repo_B_path / "refs" / "main"
        refs_main_path.parent.mkdir(parents=True)
        refs_main_path.touch()
        refs_pr_1_path = repo_B_path / "refs" / "refs" / "pr" / "1"
        refs_pr_1_path.parent.mkdir(parents=True)
        refs_pr_1_path.touch()

        # Blobs files in repo_B
        (repo_B_path / "blobs").mkdir()
        blob_1 = repo_B_path / "blobs" / "blob_1"
        blob_2 = repo_B_path / "blobs" / "blob_2"
        blob_3 = repo_B_path / "blobs" / "blob_3"
        blob_1.touch()
        blob_2.touch()
        blob_3.touch()

        # Snapshot folders in repo_B
        snapshot_1 = repo_B_path / "snapshots" / "snapshot_1"
        snapshot_2 = repo_B_path / "snapshots" / "snapshot_2"

        snapshot_1.mkdir(parents=True)
        snapshot_2.mkdir()

        # Execute deletion
        # Delete repo_A + keep only blob_1, main ref and snapshot_1 in repo_B.
        DeleteCacheStrategy(
            expected_freed_size=123456,
            blobs={blob_2, blob_3},
            refs={refs_pr_1_path},
            repos={repo_A_path},
            snapshots={snapshot_2},
        ).execute()

        # Repo A deleted
        self.assertFalse(repo_A_path.exists())
        self.assertTrue(repo_B_path.exists())

        # Only `blob` 1 remains
        self.assertTrue(blob_1.exists())
        self.assertFalse(blob_2.exists())
        self.assertFalse(blob_3.exists())

        # Only ref `main` remains
        self.assertTrue(refs_main_path.exists())
        self.assertFalse(refs_pr_1_path.exists())

        # Only `snapshot_1` remains
        self.assertTrue(snapshot_1.exists())
        self.assertFalse(snapshot_2.exists())


@pytest.mark.usefixtures("fx_cache_dir")
class TestTryDeletePath(unittest.TestCase):
    cache_dir: Path

    def test_delete_path_on_file_success(self) -> None:
        """Successfully delete a local file."""
        file_path = self.cache_dir / "file.txt"
        file_path.touch()
        _try_delete_path(file_path, path_type="TYPE")
        self.assertFalse(file_path.exists())

    def test_delete_path_on_folder_success(self) -> None:
        """Successfully delete a local folder."""
        dir_path = self.cache_dir / "something"
        subdir_path = dir_path / "bar"
        subdir_path.mkdir(parents=True)  # subfolder

        file_path_1 = dir_path / "file.txt"  # file at root
        file_path_1.touch()

        file_path_2 = subdir_path / "config.json"  # file in subfolder
        file_path_2.touch()

        _try_delete_path(dir_path, path_type="TYPE")

        self.assertFalse(dir_path.exists())
        self.assertFalse(subdir_path.exists())
        self.assertFalse(file_path_1.exists())
        self.assertFalse(file_path_2.exists())

    def test_delete_path_on_missing_file(self) -> None:
        """Try delete a missing file."""
        file_path = self.cache_dir / "file.txt"

        with self.assertLogs() as captured:
            _try_delete_path(file_path, path_type="TYPE")

        # Assert warning message with traceback for debug purposes
        self.assertEquals(len(captured.output), 1)
        self.assertTrue(
            captured.output[0].startswith(
                "WARNING:huggingface_hub.utils._cache_manager:Couldn't delete TYPE:"
                f" file not found ({file_path})\nTraceback (most recent call last):"
            )
        )

    def test_delete_path_on_missing_folder(self) -> None:
        """Try delete a missing folder."""
        dir_path = self.cache_dir / "folder"

        with self.assertLogs() as captured:
            _try_delete_path(dir_path, path_type="TYPE")

        # Assert warning message with traceback for debug purposes
        self.assertEquals(len(captured.output), 1)
        self.assertTrue(
            captured.output[0].startswith(
                "WARNING:huggingface_hub.utils._cache_manager:Couldn't delete TYPE:"
                f" file not found ({dir_path})\nTraceback (most recent call last):"
            )
        )

    def test_delete_path_on_local_folder_with_wrong_permission(self) -> None:
        """Try delete a local folder that is protected."""
        dir_path = self.cache_dir / "something"
        dir_path.mkdir()
        file_path_1 = dir_path / "file.txt"  # file at root
        file_path_1.touch()
        dir_path.chmod(444)  # Read-only folder

        with self.assertLogs() as captured:
            _try_delete_path(dir_path, path_type="TYPE")

        # Folder still exists (couldn't be deleted)
        self.assertTrue(dir_path.is_dir())

        # Assert warning message with traceback for debug purposes
        self.assertEquals(len(captured.output), 1)
        self.assertTrue(
            captured.output[0].startswith(
                "WARNING:huggingface_hub.utils._cache_manager:Couldn't delete TYPE:"
                f" permission denied ({dir_path})\nTraceback (most recent call last):"
            )
        )

        # For proper cleanup
        dir_path.chmod(509)
