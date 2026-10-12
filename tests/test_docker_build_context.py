"""Real Docker context checks with harmless fixture metadata, never credentials."""

import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import uuid


class DockerBuildContextTests(unittest.TestCase):
    """DockerBuildContextTests verifies private Git metadata is excluded from images."""

    def test_git_metadata_is_excluded_without_losing_application_source(self):
        """test_git_metadata_is_excluded_without_losing_application_source checks before/after image contents."""
        token = uuid.uuid4().hex
        images = []
        env = dict(os.environ, DOCKER_BUILDKIT="0")
        with tempfile.TemporaryDirectory(prefix="storm-context-") as folder:
            root = Path(folder)
            (root / "Dockerfile").write_text("FROM scratch\nCOPY . /payload/\n")
            (root / "app.py").write_text("print('harmless fixture')\n")
            (root / ".git").mkdir()
            (root / ".git" / "fixture-marker").write_text("not a credential\n")
            try:
                results = {}
                for stage in ("before", "after"):
                    if stage == "after":
                        (root / ".dockerignore").write_bytes(
                            (
                                Path(__file__).resolve().parents[1] / ".dockerignore"
                            ).read_bytes()
                        )
                    image = f"codex-storm-context-{token}:{stage}"
                    subprocess.run(
                        [
                            "docker",
                            "build",
                            "--network",
                            "none",
                            "--force-rm",
                            "--label",
                            f"codex.context-fixture={token}",
                            "-t",
                            image,
                            str(root),
                        ],
                        env=env,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=True,
                        timeout=60,
                    )
                    images.append(image)
                    archive = subprocess.check_output(
                        ["docker", "save", image], timeout=30
                    )
                    names = set()
                    with tarfile.open(fileobj=io.BytesIO(archive)) as outer:
                        manifest = json.load(outer.extractfile("manifest.json"))
                        for layer in manifest[0]["Layers"]:
                            with tarfile.open(
                                fileobj=io.BytesIO(outer.extractfile(layer).read())
                            ) as content:
                                names.update(x.name.rstrip("/") for x in content)
                    results[stage] = names
                self.assertIn("payload/.git/fixture-marker", results["before"])
                self.assertNotIn("payload/.git/fixture-marker", results["after"])
                self.assertIn("payload/app.py", results["after"])
            finally:
                for image in images:
                    data = json.loads(
                        subprocess.check_output(["docker", "image", "inspect", image])
                    )[0]
                    self.assertEqual(
                        data["Config"]["Labels"]["codex.context-fixture"], token
                    )
                    subprocess.run(
                        ["docker", "image", "rm", image],
                        check=True,
                        capture_output=True,
                    )


if __name__ == "__main__":
    unittest.main()
