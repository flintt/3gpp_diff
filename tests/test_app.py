import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


def sample_diff():
    return {
        "spec": "23.501",
        "old_version": "19.0.0",
        "new_version": "20.0.0",
        "old_release": 19,
        "new_release": 20,
        "title": "Test specification",
        "stats": {"added": 0, "deleted": 0, "modified": 1, "unchanged": 0},
        "clauses": [{
            "id": "1",
            "title": "1 Scope",
            "old_title": "1 Old scope",
            "level": 1,
            "status": "modified",
            "old_body": "old",
            "new_body": "new",
            "old_images": [],
            "new_images": [],
            "children": [],
        }],
    }


class FrontendCachePolicyTests(unittest.TestCase):
    def test_html_uses_fingerprinted_assets_and_bypasses_cdn_cache(self):
        client = app_module.app.test_client()

        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn(
            f"/assets/{app_module.STATIC_ASSET_VERSION}/main.css", html
        )
        self.assertIn(
            f"/assets/{app_module.STATIC_ASSET_VERSION}/app.js", html
        )
        self.assertNotIn("__STATIC_ASSET_VERSION__", html)
        self.assertEqual(
            response.headers["Cloudflare-CDN-Cache-Control"], "no-store"
        )

    def test_fingerprinted_assets_are_immutable(self):
        client = app_module.app.test_client()

        response = client.get(
            f"/assets/{app_module.STATIC_ASSET_VERSION}/app.js"
        )

        expected = "public, max-age=31536000, immutable"
        self.assertEqual(response.headers["Cache-Control"], expected)
        self.assertEqual(
            response.headers["Cloudflare-CDN-Cache-Control"], expected
        )
        response.close()

    def test_dynamic_api_bypasses_cdn_cache(self):
        client = app_module.app.test_client()

        response = client.get("/api/specs")

        self.assertEqual(
            response.headers["Cloudflare-CDN-Cache-Control"], "no-store"
        )


class ImageApiTests(unittest.TestCase):
    def test_serves_preview_inline_and_original_vector_as_download(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            image_dir = root / "cache" / "images" / "23.222" / "15.0.0"
            image_dir.mkdir(parents=True)
            (image_dir / "image7.preview.png").write_bytes(b"preview")
            (image_dir / "image7.emf").write_bytes(b"vector")
            try:
                os.chdir(root)
                client = app_module.app.test_client()
                with mock.patch.object(
                    app_module,
                    "send_from_directory",
                    return_value=app_module.Response(b"image"),
                ) as send:
                    preview = client.get(
                        "/api/image/23.222/15.0.0/image7.preview.png"
                    )
                    preview_options = send.call_args.kwargs
                    original = client.get(
                        "/api/image/23.222/15.0.0/image7.emf?download=1"
                    )
                    original_options = send.call_args.kwargs
            finally:
                os.chdir(original_cwd)

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview_options["mimetype"], "image/png")
        self.assertFalse(preview_options["as_attachment"])
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original_options["mimetype"], "image/emf")
        self.assertTrue(original_options["as_attachment"])
        self.assertEqual(original_options["download_name"], "image7.emf")


class DiffApiCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.cache_patch = mock.patch.object(
            app_module, "_diff_cache_dir", self.root / "diffs"
        )
        self.source_patch = mock.patch.object(app_module, "CACHE_DIR", self.root / "sources")
        self.cache_patch.start()
        self.source_patch.start()
        app_module._serialized_diff_cache.cache.clear()
        app_module._serialized_changes_cache.cache.clear()
        app_module._diff_search_cache.cache.clear()
        app_module._diff_locks.clear()

    def tearDown(self):
        self.source_patch.stop()
        self.cache_patch.stop()
        self.tempdir.cleanup()

    def test_reuses_precompressed_memory_and_disk_responses(self):
        client = app_module.app.test_client()
        with mock.patch.object(
            app_module, "_compute_diff_result", return_value=sample_diff()
        ) as compute:
            first = client.get(
                "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0",
                headers={"Accept-Encoding": "gzip"},
            )
            second = client.get(
                "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0",
                headers={"Accept-Encoding": "gzip"},
            )

        self.assertEqual(compute.call_count, 1)
        self.assertEqual(first.headers["X-Diff-Cache"], "computed")
        self.assertEqual(second.headers["X-Diff-Cache"], "memory")
        self.assertEqual(json.loads(gzip.decompress(first.data))["stats"]["modified"], 1)

        app_module._serialized_diff_cache.cache.clear()
        disk = client.get(
            "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0",
            headers={"Accept-Encoding": "gzip"},
        )
        self.assertEqual(disk.headers["X-Diff-Cache"], "disk")
        self.assertTrue(disk.data.startswith(b"\x1f\x8b"))

    def test_identity_and_conditional_responses(self):
        client = app_module.app.test_client()
        with mock.patch.object(
            app_module, "_compute_diff_result", return_value=sample_diff()
        ):
            response = client.get(
                "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["title"], "Test specification")
        self.assertEqual(response.headers["Cache-Control"], "private, no-cache")
        conditional = client.get(
            "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0",
            headers={"If-None-Match": response.headers["ETag"]},
        )
        self.assertEqual(conditional.status_code, 304)
        self.assertEqual(conditional.data, b"")

    def test_simultaneous_misses_share_one_computation(self):
        barrier = threading.Barrier(3)
        responses = []

        def compute(*_args):
            time.sleep(0.05)
            return sample_diff()

        def request_diff():
            with app_module.app.test_client() as client:
                barrier.wait()
                responses.append(client.get(
                    "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0",
                    headers={"Accept-Encoding": "gzip"},
                ))

        with mock.patch.object(app_module, "_compute_diff_result", side_effect=compute) as mocked:
            threads = [threading.Thread(target=request_diff) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(sorted(r.headers["X-Diff-Cache"] for r in responses), ["computed", "memory"])
        self.assertTrue(all(r.status_code == 200 for r in responses))

    def test_compact_progress_stream_does_not_repeat_the_full_payload(self):
        client = app_module.app.test_client()
        with mock.patch.object(
            app_module, "_compute_diff_result", return_value=sample_diff()
        ):
            client.get("/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0")

        stream = client.get(
            "/api/diff-stream?spec=23.501&v1=19.0.0&v2=20.0.0&compact=1"
        )
        body = stream.get_data(as_text=True)
        self.assertIn('event:done', body)
        self.assertIn('"ready":true', body)
        self.assertNotIn('"clauses"', body)

    def test_cache_write_failure_does_not_fail_the_comparison(self):
        client = app_module.app.test_client()
        with mock.patch.object(
            app_module, "_compute_diff_result", return_value=sample_diff()
        ), mock.patch.object(app_module.Path, "mkdir", side_effect=PermissionError("read only")):
            response = client.get(
                "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0",
                headers={"Accept-Encoding": "gzip"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Diff-Cache"], "computed")

    def test_changes_view_omits_only_unchanged_clause_content(self):
        payload = sample_diff()
        payload["clauses"].append({
            "id": "2",
            "title": "2 References",
            "level": 1,
            "status": "unchanged",
            "body": "large unchanged body",
            "images": [{"src": "figure.png"}],
            "children": [],
        })
        client = app_module.app.test_client()
        with mock.patch.object(
            app_module, "_compute_diff_result", return_value=payload
        ):
            initial = client.get(
                "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0&view=changes"
            )
            full = client.get(
                "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0"
            )

        initial_data = initial.get_json()
        self.assertEqual(initial_data["view"], "changes")
        self.assertEqual(initial_data["clauses"][0]["old_body"], "old")
        self.assertNotIn("body", initial_data["clauses"][1])
        self.assertNotIn("images", initial_data["clauses"][1])
        self.assertEqual(full.get_json()["clauses"][1]["body"], "large unchanged body")
        self.assertTrue(
            app_module._diff_changes_cache_path(
                "23.501", "19.0.0", "20.0.0"
            ).is_file()
        )

        app_module._serialized_changes_cache.cache.clear()
        with mock.patch.object(
            app_module,
            "_build_serialized_changes_view",
            side_effect=AssertionError("sidecar should be reused"),
        ):
            restored = client.get(
                "/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0&view=changes"
            )
        self.assertEqual(restored.get_json()["view"], "changes")

    def test_invalidates_serialized_diff_when_a_source_changes(self):
        source_dir = self.root / "sources" / "23_501" / "19.0.0"
        source_dir.mkdir(parents=True)
        source = source_dir / "source.docx"
        source.write_bytes(b"first source")
        client = app_module.app.test_client()

        with mock.patch.object(
            app_module, "_compute_diff_result", return_value=sample_diff()
        ) as compute:
            client.get("/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0")
            cache_path = app_module._diff_cache_path("23.501", "19.0.0", "20.0.0")
            newer = cache_path.stat().st_mtime_ns + 1_000_000_000
            os.utime(source, ns=(newer, newer))
            response = client.get("/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Diff-Cache"], "computed")
        self.assertEqual(compute.call_count, 2)

    def test_searches_full_text_without_returning_clause_bodies(self):
        payload = sample_diff()
        payload["clauses"].append({
            "id": "2",
            "title": "2 References",
            "level": 1,
            "status": "unchanged",
            "body": "A unique NIDD configuration phrase appears only here.",
            "images": [],
            "children": [],
        })
        client = app_module.app.test_client()
        with mock.patch.object(
            app_module, "_compute_diff_result", return_value=payload
        ):
            client.get("/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0&view=changes")

        with mock.patch.object(
            app_module,
            "_build_diff_search_index",
            wraps=app_module._build_diff_search_index,
        ) as build_index:
            old_title = client.get(
                "/api/diff-search?spec=23.501&v1=19.0.0&v2=20.0.0&q=old%20scope"
            )
            body = client.get(
                "/api/diff-search?spec=23.501&v1=19.0.0&v2=20.0.0&q=NIDD%20configuration"
            )

        self.assertEqual(old_title.get_json(), {"matches": [0], "total": 2})
        self.assertEqual(body.get_json(), {"matches": [1], "total": 2})
        self.assertNotIn(b"unique NIDD", body.data)
        self.assertEqual(body.headers["Cache-Control"], "private, no-store")
        self.assertEqual(build_index.call_count, 1)

        too_long = client.get(
            "/api/diff-search?spec=23.501&v1=19.0.0&v2=20.0.0&q=" + "x" * 513
        )
        self.assertEqual(too_long.status_code, 400)

    def test_simultaneous_searches_share_one_index_build(self):
        client = app_module.app.test_client()
        with mock.patch.object(
            app_module, "_compute_diff_result", return_value=sample_diff()
        ):
            client.get("/api/diff?spec=23.501&v1=19.0.0&v2=20.0.0")
        app_module._diff_search_cache.cache.clear()

        barrier = threading.Barrier(3)
        responses = []
        original_build = app_module._build_diff_search_index

        def build(compressed):
            time.sleep(0.05)
            return original_build(compressed)

        def search():
            with app_module.app.test_client() as thread_client:
                barrier.wait()
                responses.append(thread_client.get(
                    "/api/diff-search?spec=23.501&v1=19.0.0&v2=20.0.0&q=scope"
                ))

        with mock.patch.object(
            app_module, "_build_diff_search_index", side_effect=build
        ) as build_index:
            threads = [threading.Thread(target=search) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(build_index.call_count, 1)
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(response.get_json()["matches"] == [0] for response in responses))


class ParsedDiskCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "source.docx"
        self.source.write_bytes(b"first source")
        self.cache_patch = mock.patch.object(
            app_module, "_parsed_cache_dir", self.root / "parsed"
        )
        self.path_patch = mock.patch.object(
            app_module, "extract_doc_paths", return_value=[self.source]
        )
        self.cache_patch.start()
        self.path_patch.start()
        app_module._get_parsed_cached.cache_clear()
        app_module._parsed_locks.clear()

    def tearDown(self):
        app_module._get_parsed_cached.cache_clear()
        self.path_patch.stop()
        self.cache_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def parsed_document(title="Parsed once"):
        return {
            "title": title,
            "spec_number": "23.501",
            "version": "19.0.0",
            "release": 19,
            "clauses": [],
        }

    def test_reuses_parsed_tree_after_memory_cache_is_cleared(self):
        with mock.patch.object(
            app_module, "parse_spec", return_value=self.parsed_document()
        ) as parse:
            first = app_module._get_parsed("23.501", "19.0.0")
            app_module._get_parsed_cached.cache_clear()
            second = app_module._get_parsed("23.501", "19.0.0")

        self.assertEqual(first, second)
        self.assertEqual(parse.call_count, 1)
        self.assertTrue(app_module._parsed_cache_path("23.501", "19.0.0").is_file())

    def test_invalidates_cache_when_source_file_changes(self):
        documents = [self.parsed_document("First"), self.parsed_document("Second")]
        with mock.patch.object(app_module, "parse_spec", side_effect=documents) as parse:
            self.assertEqual(app_module._get_parsed("23.501", "19.0.0")["title"], "First")
            self.source.write_bytes(b"updated source with a new size")
            self.assertEqual(app_module._get_parsed("23.501", "19.0.0")["title"], "Second")

        self.assertEqual(parse.call_count, 2)

    def test_recovers_from_corrupted_parsed_cache(self):
        with mock.patch.object(
            app_module, "parse_spec", return_value=self.parsed_document()
        ) as parse:
            app_module._get_parsed("23.501", "19.0.0")
            app_module._get_parsed_cached.cache_clear()
            app_module._parsed_cache_path("23.501", "19.0.0").write_bytes(b"corrupt")
            recovered = app_module._get_parsed("23.501", "19.0.0")

        self.assertEqual(recovered["title"], "Parsed once")
        self.assertEqual(parse.call_count, 2)


class CachePruningTests(unittest.TestCase):
    def test_removes_only_obsolete_and_legacy_generated_caches(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            diff_root = root / "diffs"
            parsed_root = root / "parsed"
            current_diff = diff_root / f"v{app_module.DIFF_CACHE_SCHEMA}"
            previous_diff = diff_root / f"v{app_module.DIFF_CACHE_SCHEMA - 1}"
            obsolete_diff = diff_root / f"v{app_module.DIFF_CACHE_SCHEMA - 2}"
            future_diff = diff_root / f"v{app_module.DIFF_CACHE_SCHEMA + 1}"
            legacy_diff = diff_root / "23.501"
            current_parsed = parsed_root / f"v{app_module.PARSED_CACHE_SCHEMA}"
            previous_parsed = parsed_root / f"v{app_module.PARSED_CACHE_SCHEMA - 1}"
            obsolete_parsed = parsed_root / f"v{app_module.PARSED_CACHE_SCHEMA - 2}"
            for path in (
                current_diff,
                previous_diff,
                obsolete_diff,
                future_diff,
                legacy_diff,
                current_parsed,
                previous_parsed,
                obsolete_parsed,
            ):
                path.mkdir(parents=True)
                (path / "cache.bin").write_bytes(b"generated cache")

            with mock.patch.object(app_module, "_diff_cache_dir", current_diff), \
                 mock.patch.object(app_module, "_parsed_cache_dir", current_parsed):
                result = app_module.prune_obsolete_caches(retain=2)
            self.assertEqual(result["paths"], 3)
            self.assertGreater(result["bytes"], 0)
            self.assertFalse(obsolete_diff.exists())
            self.assertFalse(obsolete_parsed.exists())
            self.assertFalse(legacy_diff.exists())
            self.assertTrue(current_diff.exists())
            self.assertTrue(previous_diff.exists())
            self.assertTrue(future_diff.exists())
            self.assertTrue(current_parsed.exists())
            self.assertTrue(previous_parsed.exists())


class CrossProcessCoordinationTests(unittest.TestCase):
    @unittest.skipIf(app_module.fcntl is None, "POSIX file locking unavailable")
    def test_diff_lock_blocks_another_process_for_the_same_pair(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.object(
            app_module, "_diff_cache_dir", Path(tempdir) / "diffs"
        ):
            script = """
import sys
import time
from pathlib import Path
import app
app._diff_cache_dir = Path(sys.argv[1])
print('ready', flush=True)
started = time.monotonic()
with app._single_diff_computation('23.501@19.0.0→20.0.0'):
    print(f'acquired {time.monotonic() - started:.3f}', flush=True)
"""
            with app_module._single_diff_computation(
                "23.501@19.0.0→20.0.0"
            ):
                child = subprocess.Popen(
                    [sys.executable, "-c", script, str(Path(tempdir) / "diffs")],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertEqual(child.stdout.readline().strip(), "ready")
                time.sleep(0.12)
                self.assertIsNone(child.poll())

            stdout, stderr = child.communicate(timeout=3)

        self.assertEqual(child.returncode, 0, stderr)
        self.assertTrue(stdout.startswith("acquired "), stdout)
        self.assertGreaterEqual(float(stdout.split()[1]), 0.1)


class BackgroundTaskReservationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.task_dir_patch = mock.patch.object(
            app_module, "_task_status_dir", Path(self.tempdir.name) / "tasks"
        )
        self.task_dir_patch.start()
        app_module._release_all_task_reservations()
        app_module._download_active.clear()
        app_module._precompute_active.clear()
        app_module._download_progress.clear()
        app_module._precompute_status.clear()
        app_module._serialized_diff_cache.cache.clear()

    def tearDown(self):
        app_module._release_all_task_reservations()
        app_module._download_active.clear()
        app_module._precompute_active.clear()
        self.task_dir_patch.stop()
        self.tempdir.cleanup()

    def test_download_is_reserved_before_worker_start(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module._executor, "submit") as submit:
            first = client.post("/api/download", json={"spec": "23.501"})
            second = client.post("/api/download", json={"spec": "23.501"})

        self.assertEqual(first.get_json()["status"], "started")
        self.assertEqual(second.get_json()["status"], "already_running")
        self.assertEqual(submit.call_count, 1)

    def test_precompute_is_reserved_before_worker_start(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module._executor, "submit") as submit:
            first = client.post("/api/precompute", json={"spec": "23.501"})
            second = client.post("/api/precompute", json={"spec": "23.501"})

        self.assertEqual(first.get_json()["status"], "started")
        self.assertEqual(second.get_json()["status"], "already_running")
        self.assertEqual(submit.call_count, 1)

    def test_task_status_is_visible_without_worker_local_memory(self):
        app_module._set_task_status(
            "download",
            "23.501",
            {"status": "downloading", "done": 2, "total": 6},
            app_module._download_progress,
        )
        app_module._download_progress.clear()

        restored = app_module._read_task_status(
            "download", "23.501", app_module._download_progress
        )

        self.assertEqual(restored["status"], "downloading")
        self.assertEqual(restored["done"], 2)
        self.assertEqual(restored["total"], 6)

    def test_status_endpoint_keeps_other_worker_task_running_while_locked(self):
        other_worker_active = set()
        reservation = app_module._reserve_background_task(
            "download", "23.501", other_worker_active
        )
        self.assertIsNotNone(reservation)
        app_module._set_task_status(
            "download",
            "23.501",
            {"status": "downloading", "done": 2, "total": 6},
            {},
        )

        response = app_module.app.test_client().get(
            "/api/download-status?spec=23.501"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "downloading")
        self.assertEqual(response.get_json()["done"], 2)
        app_module._release_background_task(
            "download", "23.501", other_worker_active, reservation
        )

    def test_status_endpoint_marks_abandoned_running_task_as_error(self):
        app_module._set_task_status(
            "precompute",
            "23.501",
            {"status": "computing", "done": 1, "total": 4},
            {},
        )

        response = app_module.app.test_client().get(
            "/api/precompute-status?spec=23.501"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "error")
        self.assertIn("worker stopped", response.get_json()["error"].lower())

    def test_file_lock_blocks_an_independent_worker_reservation(self):
        first_active = set()
        second_active = set()
        first = app_module._reserve_background_task(
            "download", "23.501", first_active
        )
        self.assertIsNotNone(first)

        blocked = app_module._reserve_background_task(
            "download", "23.501", second_active
        )
        self.assertIsNone(blocked)

        app_module._release_background_task(
            "download", "23.501", first_active, first
        )
        second = app_module._reserve_background_task(
            "download", "23.501", second_active
        )
        self.assertIsNotNone(second)
        app_module._release_background_task(
            "download", "23.501", second_active, second
        )

    def test_download_completion_reports_unavailable_releases(self):
        versions = [
            {"release": 18, "version": "18.0.0"},
            {"release": 19, "version": "19.0.0"},
        ]

        def download(_spec, version):
            if version == "19.0.0":
                raise FileNotFoundError("not published")
            return Path("cached.zip")

        with mock.patch.object(app_module, "list_versions", return_value=versions), \
             mock.patch.object(app_module, "download_spec", side_effect=download), \
             mock.patch.object(app_module, "_submit_precompute") as submit:
            app_module._download_all_releases("23.501")

        status = app_module._download_progress["23.501"]
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["done"], 2)
        self.assertEqual(status["available"], 1)
        self.assertEqual(status["failed"], 1)
        submit.assert_called_once_with("23.501")

    def test_precompute_reports_partial_results_without_counting_failures_ready(self):
        versions = [
            {"release": release, "version": f"{release}.0.0"}
            for release in (18, 19, 20)
        ]

        def compute(_spec, v1, v2):
            if (v1, v2) == ("18.0.0", "20.0.0"):
                raise RuntimeError("broken source")
            return sample_diff()

        with mock.patch.object(app_module, "list_cached_versions", return_value=versions), \
             mock.patch.object(app_module, "_diff_exists_on_disk", return_value=False), \
             mock.patch.object(app_module, "_load_serialized_diff", return_value=(None, None)), \
             mock.patch.object(app_module, "_compute_diff_result", side_effect=compute) as compute_mock, \
             mock.patch.object(app_module, "_save_diff_to_disk") as save, \
             mock.patch.object(app_module.time, "sleep"):
            app_module._precompute_diffs("23.501")

        status = app_module._precompute_status["23.501"]
        self.assertEqual(status["status"], "partial")
        self.assertEqual(status["total"], 3)
        self.assertEqual(status["processed"], 3)
        self.assertEqual(status["done"], 2)
        self.assertEqual(status["failed"], 1)
        self.assertEqual(compute_mock.call_count, 3)
        self.assertEqual(save.call_count, 2)
        self.assertEqual(
            [call.args[1:] for call in compute_mock.call_args_list],
            [
                ("19.0.0", "20.0.0"),
                ("18.0.0", "19.0.0"),
                ("18.0.0", "20.0.0"),
            ],
        )

    def test_precompute_rechecks_cache_after_acquiring_pair_lock(self):
        versions = [
            {"release": release, "version": f"{release}.0.0"}
            for release in (19, 20)
        ]
        with mock.patch.object(app_module, "list_cached_versions", return_value=versions), \
             mock.patch.object(app_module, "_diff_exists_on_disk", return_value=False), \
             mock.patch.object(app_module, "_load_serialized_diff", return_value=(b"ready", "memory")), \
             mock.patch.object(app_module, "_compute_diff_result") as compute, \
             mock.patch.object(app_module.time, "sleep"):
            app_module._precompute_diffs("23.501")

        self.assertEqual(app_module._precompute_status["23.501"]["status"], "completed")
        self.assertEqual(app_module._precompute_status["23.501"]["done"], 1)
        compute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
