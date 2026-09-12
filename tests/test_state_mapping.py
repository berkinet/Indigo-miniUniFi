import importlib.util
import pathlib
import unittest


MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / 'miniUniFi.indigoPlugin/Contents/Server Plugin/state_mapping.py'
)
spec = importlib.util.spec_from_file_location('state_mapping_under_test', MODULE_PATH)
state_mapping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state_mapping)


class StateMappingTests(unittest.TestCase):
    def as_dict(self, payload, **kwargs):
        return {
            state['key']: state['value']
            for state in state_mapping.flatten_states(payload, **kwargs)
        }

    def test_preserves_false_zero_float_zero_and_empty_string(self):
        states = self.as_dict({
            'disabled': False,
            'clients': 0,
            'rate': 0.0,
            'label': '',
            'missing': None,
        })
        self.assertIs(states['disabled'], False)
        self.assertEqual(states['clients'], 0)
        self.assertEqual(states['rate'], 0.0)
        self.assertEqual(states['label'], '')
        self.assertNotIn('missing', states)

    def test_sanitizes_punctuation_and_preserves_legacy_numeric_prefix(self):
        states = self.as_dict({'tx_bytes-r': 1, ' user name ': 'rick', '1st': True})
        self.assertEqual(states['tx_bytes_r'], 1)
        self.assertEqual(states['user_name'], 'rick')
        self.assertTrue(states['sk1st'])

    def test_empty_key_is_safe(self):
        self.assertEqual(self.as_dict({'': 7}), {'state': 7})

    def test_structural_collisions_receive_stable_unique_suffixes(self):
        first = state_mapping.flatten_states({'a-b': 1, 'a_b': 2})
        second = state_mapping.flatten_states({'a_b': 2, 'a-b': 1})
        self.assertEqual(first, second)
        self.assertEqual(len({item['key'] for item in first}), 2)
        self.assertTrue(all(item['key'].startswith('a_b_') for item in first))

    def test_nested_paths_that_flatten_the_same_are_distinguished(self):
        states = state_mapping.flatten_states({'a_b': {'c': 1}, 'a': {'b_c': 2}})
        self.assertEqual(len({item['key'] for item in states}), 2)
        self.assertTrue(all(item['key'].startswith('a_b_c_') for item in states))

    def test_static_state_names_are_reserved(self):
        states = state_mapping.flatten_states(
            {'offline_seconds': 99}, reserved_keys={'offline_seconds'})
        self.assertNotEqual(states[0]['key'], 'offline_seconds')
        self.assertTrue(states[0]['key'].startswith('offline_seconds_'))

    def test_reserved_digest_candidate_is_resolved_without_looping(self):
        first = state_mapping.flatten_states({'status': 'api'}, reserved_keys={'status'})
        second = state_mapping.flatten_states(
            {'status': 'api'}, reserved_keys={'status', first[0]['key']})
        self.assertNotEqual(second[0]['key'], first[0]['key'])
        self.assertTrue(second[0]['key'].startswith(first[0]['key']))

    def test_list_order_is_encoded_in_keys(self):
        states = self.as_dict({'radios': [{'channel': 1}, {'channel': 36}]})
        self.assertEqual(states['radios_0_channel'], 1)
        self.assertEqual(states['radios_1_channel'], 36)


if __name__ == '__main__':
    unittest.main()
