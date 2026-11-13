from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

from motor_platform import CanFrame
from scripts.factory_test import firmware as fw
from scripts.factory_test import firmware_library as library
from scripts.factory_test.lifecycle import StationInstance
from scripts.factory_test.station import DemoServices, StationRunner
from scripts.factory_test.station_config import StationConfig


def image_bytes(platform,version):
    data=bytearray(384 if platform=='GD' else 49152)
    if platform=='GD': struct.pack_into('<II',data,0,0x20007000,0x08000021)
    struct.pack_into('<H',data,len(data)-128,version)
    data[-40:-32]=b'BPKR128\0'
    struct.pack_into('<I',data,len(data)-4,zlib.crc32(data[:-4]))
    return bytes(data)


class Drive:
    def __init__(self,platform,before):
        self.platform,self.version=platform,before
        self.sn='000000000000'+('0111' if platform=='GD' else '0221')+'0000000000'
        self._bus=type('Bus',(),{})()
        self._bus.transport=self
        self.disable=Mock()
        self.frames=[]
        self.received=bytearray()
        self.fail_at=self.drop_commit=None
        self.ack=None
        self.applied=False
        self.wait=Mock()
        self.check_stop=Mock()

    def discover(self,*,exhaustive):
        assert exhaustive
        return dict(node_id=1,sn=self.sn)

    @contextmanager
    def connect(self,node):
        assert node==1
        yield self

    def motor(self,node):
        return self

    def read_serial_bytes(self,timeout):
        return self.sn.encode()

    def read(self,bench,node,index,size):
        if index==0x2100: return self.version
        if index=={'GD':0x2001,'ADM':0x2072}[self.platform]: return 1
        raise RuntimeError('SDO rejected')

    def send(self,frame):
        self.frames.append(frame)
        assert frame.can_id==0x681 and not frame.is_extended and frame.is_fd
        if frame.data==fw.START:
            self.ack=fw.START
        elif frame.data==fw.COMMIT:
            assert len(self.received)>0 and zlib.crc32(self.received[:-4])==int.from_bytes(self.received[-4:],'little')
            self.applied=True
            self.version=int.from_bytes(self.received[-128:-126],'little')
            self.ack=None if self.drop_commit else fw.COMMIT
        else:
            assert len(frame.data)==8
            self.ack=bytes(4) if len(self.received)==self.fail_at else b''
            self.received.extend(frame.data)

    def recv(self,timeout):
        if self.ack is None: return None
        data,self.ack=self.ack,None
        return CanFrame(0x681,data)


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def setup_drive(self,family,version=245,before=240):
        folder=self.root/family/'APP'
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/f'motor_app_{version}.bin'
        data=image_bytes(family,version)
        path.write_bytes(data)
        hardware='0111' if family=='GD' else '0221'
        config=StationConfig(firmware_directory=str(self.root),hardware_version=hardware,model=hardware[1:])
        return config,Drive(family,before),path,data

    def test_numeric_latest_exact_file_validation_and_staged_replacement(self):
        config,_,path,data=self.setup_drive('ADM')
        (path.parent/'motor_app_99.bin').write_bytes(b'old')
        (path.parent/'motor_app_999.bin.tmp').write_bytes(b'in progress')
        self.assertEqual(fw.app_target(config),(245,path))
        self.assertEqual(fw.read_image(path,245,'ADM')[0],data)
        with self.assertRaisesRegex(fw.FirmwareError,'版本不一致'): fw.read_image(path,244,'ADM')
        (path.parent/'second_245.bin').write_bytes(data)
        with self.assertRaisesRegex(fw.FirmwareError,'多个'): fw.app_target(config)
        path.write_bytes(data[:-8])
        with self.assertRaisesRegex(fw.FirmwareError,'布局'): fw.read_image(path,245,'ADM')

    def test_full_transfer_both_platforms_same_can_messages_and_exact_readback(self):
        for family in ('GD','ADM'):
            config,drive,_,data=self.setup_drive(family)
            result=fw.check_firmware(config,drive,1,drive.sn,Mock())
            self.assertEqual((result['before'],result['after'],result['outcome']),(240,245,'UPDATED'))
            self.assertEqual(drive.received,data)
            self.assertEqual(len(drive.frames),len(data)//8+2)
            self.assertIsNone(result['device_boot_version'])
            drive.disable.assert_called_once()

    def test_current_and_newer_do_not_touch_flash_or_motor_enable(self):
        config,drive,_,_=self.setup_drive('GD')
        for version,outcome in ((245,'CURRENT'),(300,'NEWER')):
            drive.version=version
            self.assertEqual(fw.check_firmware(config,drive,1,drive.sn,Mock())['outcome'],outcome)
        self.assertEqual(drive.frames,[]);drive.disable.assert_not_called()

    def test_corrupt_file_wrong_platform_and_wrong_incoming_id_all_stop_before_erase(self):
        config,drive,path,data=self.setup_drive('GD')
        with self.assertRaisesRegex(fw.FirmwareError,'身份'): fw.check_firmware(config,drive,1,'changed',Mock())
        drive.platform='ADM'
        with self.assertRaisesRegex(fw.FirmwareError,'无法确认'): fw.check_firmware(config,drive,1,drive.sn,Mock())
        path.write_bytes(data[:-1]+bytes([data[-1]^1]))
        with self.assertRaisesRegex(fw.FirmwareError,'CRC32'): fw.check_firmware(config,drive,1,drive.sn,Mock())
        self.assertEqual(drive.frames,[]);drive.disable.assert_not_called()

    def test_nack_preserves_exact_offset_and_never_replays(self):
        config,drive,_,_=self.setup_drive('GD')
        drive.fail_at=16
        with self.assertRaises(fw.FirmwareError) as failure: fw.check_firmware(config,drive,1,drive.sn,Mock())
        self.assertEqual(failure.exception.diagnostic_code,'FW_ACK_REJECTED')
        self.assertEqual(failure.exception.diagnostic_context['confirmed_bytes'],16)
        self.assertEqual(len(drive.frames),4);self.assertFalse(drive.applied)

    def test_lost_final_ack_resolves_only_with_identity_and_version_readback(self):
        config,drive,_,_=self.setup_drive('GD')
        drive.drop_commit=True
        with patch.object(fw.time,'monotonic',side_effect=(i/100 for i in range(10000))):
            result=fw.check_firmware(config,drive,1,drive.sn,Mock())
        self.assertFalse(result['commit_ack']);self.assertEqual(result['after'],245)
        self.assertEqual(sum(frame.data==fw.COMMIT for frame in drive.frames),1)

    def test_data_ack_timeout_does_not_resend_or_commit(self):
        config,drive,_,_=self.setup_drive('GD')
        receive=drive.recv
        drive.recv=lambda timeout: None if len(drive.received)==16 else receive(timeout)
        with patch.object(fw.time,'monotonic',side_effect=(i/100 for i in range(10000))):
            with self.assertRaises(fw.FirmwareError) as failure: fw.check_firmware(config,drive,1,drive.sn,Mock())
        self.assertEqual(failure.exception.diagnostic_code,'FW_ACK_TIMEOUT')
        self.assertEqual(failure.exception.diagnostic_context['confirmed_bytes'],8)
        self.assertEqual(len(drive.frames),3)
        self.assertFalse(drive.applied)

    def test_operator_stop_during_transfer_sends_no_following_frame(self):
        config,drive,_,_=self.setup_drive('ADM')
        def stop():
            if drive.received: raise InterruptedError('stop')
        drive.check_stop=stop
        with self.assertRaises(InterruptedError): fw.check_firmware(config,drive,1,drive.sn,Mock())
        self.assertEqual(len(drive.received),8)
        self.assertFalse(drive.applied)

    def test_wrong_or_missing_readback_never_completes_the_step(self):
        for wrong_sn in (False,True):
            config,drive,_,_=self.setup_drive('GD')
            raw_read=drive.read
            drive.read=lambda b,n,i,s: 244 if drive.applied and i==0x2100 else raw_read(b,n,i,s)
            drive.read_serial_bytes=lambda timeout: b'another' if drive.applied and wrong_sn else drive.sn.encode()
            with patch.object(fw.time,'monotonic',side_effect=(i/100 for i in range(50000))):
                with self.assertRaises(fw.FirmwareError) as failure: fw.check_firmware(config,drive,1,drive.sn,Mock())
            self.assertEqual(failure.exception.diagnostic_code,'FW_IDENTITY_CHANGED' if wrong_sn else 'FW_READBACK_UNCONFIRMED')

    def test_firmware_failure_prevents_id_calibration_mes_print_and_upload(self):
        config=StationConfig(simulation=True)
        service=DemoServices(config,lambda:False,lambda *args:None)
        service.firmware=Mock(side_effect=fw.FirmwareError('FW_IMAGE_CRC','CRC 错误'))
        for name in ('assign_id','calibrate','allocate','print_sn','upload'):
            setattr(service,name,Mock())
        runner=StationRunner(config,root=self.root,services=service)
        result=runner.begin('incoming',1)
        self.assertEqual(result['steps'],{'firmware':'FAILED'})
        self.assertEqual(result['diagnostic']['code'],'FW_IMAGE_CRC')
        for name in ('assign_id','calibrate','allocate','print_sn','upload'): getattr(service,name).assert_not_called()


class FirmwareLibraryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = StationConfig(credentials_file=str(self.root/'profile/station.json'))
        self.package = self.root/'software/firmware'
        packaged = patch.object(library, 'package_firmware', return_value=self.package)
        packaged.start()
        self.addCleanup(packaged.stop)

    def image(self, version, *, platform='ADM', folder=None, name=None):
        folder = folder or self.root/'usb'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/(name or f'drive_app_{version}.bin')
        path.write_bytes(image_bytes(platform, version))
        return path

    def test_candidates_never_arm_empty_station_and_legacy_highest_stays_usable(self):
        self.image(245, folder=self.package/'候选/ADM')
        self.assertEqual(library.selections(self.config), (None, None))
        legacy = self.image(190, folder=self.package/'ADM/APP')
        self.image(12, folder=legacy.parent)
        self.assertEqual(fw.app_target(self.config),(190, legacy))
        new, old = library.change_app(self.config,self.image(245))
        self.assertTrue(old.sha256)
        self.assertEqual(old.version,190)
        self.assertNotEqual(old.path,legacy)
        legacy.unlink()
        self.image(999,folder=self.package/'ADM/APP')
        self.assertEqual(fw.app_target(self.config),(245,new.path))
        self.assertEqual(library.change_app(self.config,restore=old),(old,new))

    def test_install_from_removable_source_persists_across_program_upgrade_and_restart(self):
        source = self.image(245)
        data = source.read_bytes()
        new, previous = library.change_app(self.config, source)
        self.assertIsNone(previous)
        source.unlink()
        switched = self.root/'new-software/firmware'
        self.image(401,folder=switched/'候选/ADM')
        with patch.object(library,'package_firmware',return_value=switched):
            reloaded = StationConfig(credentials_file=self.config.credentials_file)
            self.assertTrue(new.path.is_relative_to(self.root/'profile/firmware'))
            self.assertEqual(library.selections(reloaded), (new,None))
            self.assertEqual(library.load_target(reloaded)[0], data)

    def test_recent_and_previous_are_platform_specific_and_custom_library_is_preserved(self):
        custom = replace(self.config,firmware_directory=str(self.root/'custom'))
        old, _ = library.change_app(custom,self.image(240))
        new, prev = library.change_app(custom,self.image(245))
        self.assertEqual(prev, old)
        gd = replace(custom, hardware_version='0111', model='111')
        gd_app, _ = library.change_app(gd,self.image(143,platform='GD'))
        self.assertEqual(library.change_app(custom,restore=old),(old,new))
        self.assertEqual(library.selections(gd),(gd_app,None))
        self.assertEqual(library.selections(self.config),(None,None))

    def test_invalid_filename_crc_wrong_platform_or_incomplete_write_keeps_old_selection(self):
        old, _ = library.change_app(self.config,self.image(100))
        wrong_name=self.image(101,name='missing_version.bin')
        crc=self.image(101)
        damaged=bytearray(crc.read_bytes()); damaged[250]^=1; crc.write_bytes(damaged)
        for bad in (wrong_name,crc,self.image(143,platform='GD'),self.image(100,name='drive_boot_100.bin'),
                    self.image(101,name='version_mismatch_102.bin')):
            with self.subTest(bad=bad.name),self.assertRaises(fw.FirmwareError):
                library.change_app(self.config,bad)
            self.assertEqual(library.selections(self.config),(old,None))
        for operation in ('atomic_json','atomic_replace'):
            candidate=self.image(102 if operation=='atomic_json' else 103)
            with patch.object(library,operation,side_effect=PermissionError('只读目录')), self.assertRaises(OSError):
                library.change_app(self.config,candidate)
            self.assertEqual(library.selections(self.config),(old,None))
            self.assertEqual(library.load_target(self.config)[0],old.path.read_bytes())

    def test_same_version_different_contents_remain_distinct_and_reimport_is_idempotent(self):
        path=self.image(245)
        first,_=library.change_app(self.config,path)
        changed=bytearray(path.read_bytes()); changed[200]=16
        struct.pack_into('<I',changed,len(changed)-4,zlib.crc32(changed[:-4])); path.write_bytes(changed)
        second,prev=library.change_app(self.config,path)
        self.assertEqual(prev,first)
        self.assertNotEqual(first.sha256,second.sha256)
        self.assertEqual(library.change_app(self.config,path),(second,first))
        self.assertEqual(library.change_app(self.config,restore=first),(first,second))

    def test_mismatched_fingerprint_never_silently_switches_to_manual_directory(self):
        path=self.image(245)
        active,_=library.change_app(self.config,path)
        other=bytearray(path.read_bytes()); other[0]=1
        struct.pack_into('<I',other,len(other)-4,zlib.crc32(other[:-4]))
        active.path.write_bytes(other)
        self.image(500,folder=self.package/'ADM/APP')
        with self.assertRaises(fw.FirmwareError) as mismatch:
            library.load_target(self.config)
        self.assertEqual(mismatch.exception.diagnostic_code,'FW_SELECTION_HASH')
        drive=Drive('ADM',1)
        with self.assertRaises(fw.FirmwareError):
            fw.check_firmware(self.config,drive,1,drive.sn,Mock())
        self.assertEqual(drive.frames,[])

    def test_stale_start_digest_or_rollback_snapshot_never_acts_on_a_new_image(self):
        old,_=library.change_app(self.config,self.image(143))
        target,_=library.change_app(self.config,self.image(245))
        library.change_app(self.config,self.image(246))
        with self.assertRaises(fw.FirmwareError) as error:
            library.change_app(self.config,restore=old)
        self.assertEqual(error.exception.diagnostic_code,'FW_PREVIOUS_CHANGED')
        drive=Drive('ADM',1)
        with self.assertRaises(fw.FirmwareError) as error:
            fw.check_firmware(self.config,drive,1,drive.sn,Mock(),expected_sha256=target.sha256)
        self.assertEqual(error.exception.diagnostic_code,'FW_TARGET_CHANGED')
        self.assertEqual(drive.frames,[])
        drive.disable.assert_not_called()

    def test_locked_shared_library_or_corrupt_index_can_be_recovered_without_losing_file(self):
        old,_=library.change_app(self.config,self.image(143))
        folder=library.firmware_root(self.config)/'ADM/APP'
        with StationInstance(folder,lock_name='.换版.lock'),self.assertRaises(fw.FirmwareError) as busy:
            library.change_app(self.config,self.image(144))
        self.assertEqual(busy.exception.diagnostic_code,'FW_LIBRARY_BUSY')
        index=folder/'当前版本.json'
        doc=json.loads(index.read_bytes())
        doc['current']['name']='../another_143.bin'
        index.write_text(json.dumps(doc),encoding='utf-8')
        with self.assertRaises(fw.FirmwareError):
            library.load_target(self.config)
        repaired,_=library.change_app(self.config,self.image(144))
        self.assertEqual(repaired.version,144)
        self.assertTrue(old.path.is_file())
        self.assertEqual(len(list(folder.glob('索引备份-*.json'))),1)

    def test_can_transfer_uses_frozen_bytes_if_shared_library_changes_during_upgrade(self):
        selected,_=library.change_app(self.config,self.image(245))
        next_image=self.image(246)
        drive=Drive('ADM',240)
        def update_during_transfer(message):
            if message=='写入 APP 10%':
                library.change_app(self.config,next_image)
        info=fw.check_firmware(self.config,drive,1,drive.sn,update_during_transfer,expected_sha256=selected.sha256)
        self.assertEqual((info['after'],info['sha256']),(245,selected.sha256))
        self.assertEqual(drive.received,selected.path.read_bytes())
        self.assertEqual(fw.app_target(self.config)[0],246)
