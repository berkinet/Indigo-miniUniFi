import pathlib
import unittest


PLUGIN_SERVER = (
    pathlib.Path(__file__).parents[1]
    / 'miniUniFi.indigoPlugin/Contents/Server Plugin'
)


class PackagingTests(unittest.TestCase):
    def test_plugin_has_no_external_requirements_file(self):
        self.assertFalse((PLUGIN_SERVER / 'requirements.txt').exists())


if __name__ == '__main__':
    unittest.main()
