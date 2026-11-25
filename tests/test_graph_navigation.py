from pathlib import Path
import shutil
import subprocess
import unittest


class GraphNavigationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Browser script checks require Node.js')
    def test_embedded_instance_navigation_and_rendering(self):
        script = Path(__file__).with_suffix('.js')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
