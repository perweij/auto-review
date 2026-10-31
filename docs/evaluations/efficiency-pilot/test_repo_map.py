import pathlib
import subprocess
import tempfile
import unittest
from repo_map import generate


class MapTest(unittest.TestCase):
    def test_current_source_and_incomplete_files(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            subprocess.run(['git', 'init', '-q', directory], check=True)
            source = repo / 'example.py'
            source.write_text('def original(value):\n    return value\n')
            (repo/'broken.py').write_text('invalid python !')
            (repo/'gone.py').write_text('def gone(): pass\n')
            (repo/'external.py').symlink_to(source)
            subprocess.run(['git','add','.'],cwd=repo,check=True)
            (repo/'gone.py').unlink()
            first=generate(repo,'example')
            self.assertEqual(first,generate(repo,'example'))
            self.assertIn('original(value)',first)
            self.assertNotIn('broken.py',first)
            self.assertNotIn('gone.py',first)
            self.assertNotIn('external.py',first)
            source.write_text('def revised(other):\n    return other\n')
            updated=generate(repo,'example')
            self.assertIn('revised(other)',updated)
            self.assertNotIn('original',updated)
            self.assertLessEqual(len(generate(repo,'example',budget=180)),180)

    def test_large_map_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=pathlib.Path(directory)
            subprocess.run(['git','init','-q',directory],check=True)
            for n in range(40):
                (repo/f'module{n}.py').write_text('\n'.join(f'def symbol{i}(arg): pass' for i in range(80)))
            subprocess.run(['git','add','.'],cwd=repo,check=True)
            output=generate(repo,'module')
            self.assertLessEqual(len(output),6000)
            self.assertIn('additional symbols omitted',output)
            self.assertGreater(output.count('.py'),3)


if __name__ == '__main__':
    unittest.main()
