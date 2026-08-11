import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import spec_fetcher


class AtomicDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache_patch = mock.patch.object(
            spec_fetcher, "CACHE_DIR", Path(self.tempdir.name)
        )
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.tempdir.cleanup()

    def test_publishes_only_a_valid_completed_zip(self):
        def write_zip(_url, path, timeout=120):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("23501-j00.docx", b"document")

        with mock.patch.object(spec_fetcher, "_curl_binary", side_effect=write_zip):
            path = spec_fetcher.download_spec("23.501", "19.0.0")

        self.assertTrue(path.is_file())
        self.assertTrue(spec_fetcher._is_valid_zip(path))
        self.assertEqual(list(path.parent.glob("*.part")), [])

    def test_removes_partial_file_after_an_invalid_download(self):
        def write_invalid(_url, path, timeout=120):
            path.write_bytes(b"not a zip")

        with mock.patch.object(spec_fetcher, "_curl_binary", side_effect=write_invalid):
            with self.assertRaises(FileNotFoundError):
                spec_fetcher.download_spec("23.501", "19.0.0")

        cache_dir = Path(self.tempdir.name) / "23_501"
        self.assertEqual(list(cache_dir.iterdir()), [])

    def test_prefers_an_already_converted_docx(self):
        extract_dir = Path(self.tempdir.name) / "23_501" / "19.0.0"
        extract_dir.mkdir(parents=True)
        (extract_dir / "spec.doc").write_bytes(b"legacy")
        docx = extract_dir / "spec.docx"
        docx.write_bytes(b"converted")

        with mock.patch.object(
            spec_fetcher, "download_spec", return_value=Path("unused.zip")
        ):
            selected = spec_fetcher.extract_doc_path("23.501", "19.0.0")

        self.assertEqual(selected, docx)

    def test_returns_every_split_document_in_filename_order(self):
        extract_dir = Path(self.tempdir.name) / "29_522" / "19.0.0"
        extract_dir.mkdir(parents=True)
        cover = extract_dir / "spec_0_cover.docx"
        main = extract_dir / "spec_1_main.docx"
        annex = extract_dir / "spec_2_annex.docx"
        for path in (annex, main, cover):
            path.write_bytes(path.name.encode())

        with mock.patch.object(
            spec_fetcher, "download_spec", return_value=Path("unused.zip")
        ):
            selected = spec_fetcher.extract_doc_paths("29.522", "19.0.0")

        self.assertEqual(selected, [cover, main, annex])

    def test_sorts_numbered_document_parts_naturally(self):
        extract_dir = Path(self.tempdir.name) / "29_522" / "19.0.0"
        extract_dir.mkdir(parents=True)
        part_two = extract_dir / "spec_part_2.docx"
        part_ten = extract_dir / "spec_part_10.docx"
        for path in (part_ten, part_two):
            path.write_bytes(path.name.encode())

        with mock.patch.object(
            spec_fetcher, "download_spec", return_value=Path("unused.zip")
        ):
            selected = spec_fetcher.extract_doc_paths("29.522", "19.0.0")

        self.assertEqual(selected, [part_two, part_ten])


if __name__ == "__main__":
    unittest.main()
