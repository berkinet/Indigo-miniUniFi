import pathlib
import unittest
import xml.etree.ElementTree as ET


DEVICES_XML = (
    pathlib.Path(__file__).parents[1]
    / 'miniUniFi.indigoPlugin/Contents/Server Plugin/Devices.xml'
)


class DeviceXmlTests(unittest.TestCase):
    def test_every_state_has_required_indigo_labels(self):
        root = ET.parse(DEVICES_XML).getroot()
        for device in root.findall('Device'):
            for state in device.findall('./States/State'):
                with self.subTest(device=device.get('id'), state=state.get('id')):
                    self.assertIsNotNone(state.find('TriggerLabel'))
                    self.assertIsNotNone(state.find('ControlPageLabel'))
                    self.assertTrue((state.findtext('TriggerLabel') or '').strip())
                    self.assertTrue((state.findtext('ControlPageLabel') or '').strip())


if __name__ == '__main__':
    unittest.main()
