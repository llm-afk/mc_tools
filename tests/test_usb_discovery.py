import ctypes as C
import os
import unittest
from unittest.mock import patch

from motor_platform.transports import usb_discovery as usb


GUID = '{c15b4308-04d3-11e6-b3ea-6057189e6443}'
PREFIX = 'USB\\VID_1D50&PID_606F&MI_00\\'


class FakeApi:
    def __init__(self, instances):
        self.instances = instances
        self.destroyed = False
        self.parent_error = 0
        self.size_calls = 0

    def create(self, guid, enumerator, window, flags):
        assert guid is None and enumerator == 'USB' and flags == 6
        return 123

    def enumerate(self, handle, index, info):
        assert handle == 123
        if index == len(self.instances):
            C.set_last_error(259)
            return False
        info._obj.node = index
        return True

    def instance_id(self, handle, info, text, size, required):
        value = self.instances[info._obj.node]
        if size <= len(value):
            required._obj.value = len(value)+1
            C.set_last_error(122)
            return False
        text.value = value
        return True

    def parent(self, target, node, flags):
        target._obj.value = node
        return self.parent_error

    def identity(self, node):
        return 'USB\\VID_1D50&PID_606F\\SERIAL'+str(node)

    def id_size(self, target, node, flags):
        self.size_calls += 1
        target._obj.value = len(self.identity(node))
        return 0

    def node_id(self, node, text, size, flags):
        text.value = self.identity(node)
        return 0

    def destroy(self, handle):
        assert handle == 123
        self.destroyed = True


@unittest.skipUnless(os.name == 'nt', 'Windows ABI and last-error conventions')
class DiscoveryTests(unittest.TestCase):
    def test_lists_only_present_native_interfaces_and_keeps_path_contract(self):
        api = FakeApi(['USB\\VID_16D0&PID_117E\\slcan',
                       'USB\\VID_1D50&PID_606F&MI_01\\update', PREFIX+'first', PREFIX+'second'])
        with patch.object(usb, '_Api', return_value=api), patch.object(usb, '_interface_guid', return_value=GUID) as guid:
            found = usb.detect_canables()
        self.assertEqual([item['serial_number'] for item in found], ['SERIAL2','SERIAL3'])
        self.assertEqual(found[0]['path'], '\\\\?\\'+(PREFIX+'first').replace('\\','#')+'#'+GUID)
        self.assertEqual(guid.call_count, 2)
        self.assertTrue(api.destroyed)

    def test_long_instance_buffer_is_resized_without_truncation(self):
        instance = PREFIX+'X'*300
        api = FakeApi([instance])
        with patch.object(usb, '_Api', return_value=api), patch.object(usb, '_interface_guid', return_value=GUID):
            found = usb.detect_canables()
        self.assertIn('X'*300, found[0]['path'])
        self.assertTrue(api.destroyed)

    def test_parent_lookup_failure_does_not_query_invalid_parent_and_closes_set(self):
        api = FakeApi([PREFIX+'device'])
        api.parent_error = 13
        with patch.object(usb, '_Api', return_value=api):
            with self.assertRaisesRegex(OSError, 'CM_Get_Parent'):
                usb.detect_canables()
        self.assertEqual(api.size_calls, 0)
        self.assertTrue(api.destroyed)

    def test_driver_registry_failure_preserves_error_and_closes_set(self):
        api = FakeApi([PREFIX+'device'])
        with patch.object(usb, '_Api', return_value=api), \
             patch.object(usb, '_interface_guid', side_effect=PermissionError('registry denied')):
            with self.assertRaisesRegex(PermissionError, 'registry denied'):
                usb.detect_canables()
        self.assertTrue(api.destroyed)
