"""Exercise the PC refiner's real ISO checksum mapping on tiny local fixtures."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'build/refinalize-installer-amd64.sh'


@unittest.skipUnless(shutil.which('xorriso'), 'xorriso is required for the real ISO round trip')
class InstallerManifestRefinalizeTests(unittest.TestCase):
    def refinalize_fixture(self, omit_manifest_map=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        work, previous, output, stage = [root / name for name in ('work', 'previous', 'output', 'stage')]
        for path in (work, previous, output, stage):
            path.mkdir()
        binary = work / 'binary'
        (binary / 'live').mkdir(parents=True)
        old = b'old source bytes' * 100
        new = b'new complete source bytes' * 100
        (binary / 'live/filesystem.squashfs').write_bytes(old)
        (binary / 'vaultboot-squashfs.sha256').write_text(
            hashlib.sha256(old).hexdigest() + '  live/filesystem.squashfs\n')
        subprocess.run(['xorriso', '-as', 'mkisofs', '-quiet', '-o', str(previous / 'fixture.iso'),
                        str(binary)], check=True, capture_output=True)
        (stage / 'final.squashfs').write_bytes(new)
        script = SCRIPT.read_text()
        start = script.index('mv "$stage/final.squashfs" binary/live/filesystem.squashfs')
        end = script.index('\npython3 /src/scripts/make-usb-image.py', start)
        commands = script[start:end]
        commands = commands.replace('/work/', str(work) + '/').replace('/previous/', str(previous) + '/').replace('/out/', str(output) + '/')
        if omit_manifest_map:
            commands = commands.replace(
                f'checksum_maps=(-map {work}/binary/vaultboot-squashfs.sha256 /vaultboot-squashfs.sha256)',
                'checksum_maps=()')
        # Debian live-build itself is outside this focused test. Its checksum
        # step is simulated; xorriso and the completed-ISO verification are real.
        prefix = '''set -Eeuo pipefail
lb() {
    [[ "$*" == 'binary_checksums --force' ]]
    (cd binary && sha256sum live/filesystem.squashfs vaultboot-squashfs.sha256 > sha256sum.txt)
}
'''
        result = subprocess.run(['bash', '-c', prefix + commands], cwd=work,
                                env={**os.environ, 'stage': str(stage), 'prefix': 'fixture'},
                                text=True, capture_output=True)
        return result, work, stage, new

    def test_completed_iso_contains_new_source_and_matching_installer_manifest(self):
        result, work, stage, new = self.refinalize_fixture()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((stage / 'iso-check/live/filesystem.squashfs').read_bytes(), new)
        manifest = (stage / 'iso-check/vaultboot-squashfs.sha256').read_bytes()
        self.assertEqual(manifest.decode(), hashlib.sha256(new).hexdigest() + '  live/filesystem.squashfs\n')
        self.assertIn(hashlib.sha256(manifest).hexdigest() + '  vaultboot-squashfs.sha256',
                      (work / 'binary/sha256sum.txt').read_text())

    def test_completed_iso_gate_rejects_stale_manifest_if_mapping_is_omitted(self):
        result, _, _, _ = self.refinalize_fixture(omit_manifest_map=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('live/filesystem.squashfs: FAILED', result.stdout)


if __name__ == '__main__':
    unittest.main()
