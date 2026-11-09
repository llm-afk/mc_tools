import copy
from dataclasses import asdict, replace
import io
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from urllib.error import HTTPError, URLError

from motor_platform import Bench, CanFrame, MotorBus, SdoError
from scripts.factory_test.station_config import (StationConfig, load_config, save_config,
                                                hardware_parts, hardware_input_value, temporary_sn)
from scripts.factory_test.integrations import (MesClient, PrinterClient, PrintUnknown,
    IntegrationError, PendingConfiguration, mes_payload)
from scripts.factory_test.maintenance import Maintenance
from scripts.factory_test.station import StationRunner, DemoServices, PresenceLatch, RecoveryRequired
from tests.test_motor import FakeTransport


SN = 'C2KNEE0STDXC02212610280001'


class ConfigTests(unittest.TestCase):
    def test_hardware_fields_accept_typed_code_or_dropdown_description(self):
        for text in ('2',' 2 ','2 · ADM 平台','2·ADM 平台'):
            self.assertEqual(hardware_input_value('B',text),'2')
        self.assertEqual(hardware_input_value('A','1'),'1')
        self.assertEqual(hardware_input_value('D','3'),'3')
        for invalid in ('','22','0221','!','GD'):
            with self.assertRaises(ValueError): hardware_input_value('B',invalid)

    def test_typed_production_date_is_checked_before_any_live_operation(self):
        for value in ('2611','260230','261302','not-a-date'):
            with self.assertRaisesRegex(ValueError,'生产日期'):
                StationConfig(production_date=value).validate()
        StationConfig(production_date='261102').validate()
        StationConfig(production_date='').validate()

    def test_temporary_sn_has_only_hardware_and_ascii_zeroes_for_all_defined_types(self):
        for platform in '12':
            for motor in '123456':
                for revision in '12':
                    hardware='0'+platform+motor+revision
                    value=temporary_sn(hardware)
                    self.assertEqual(len(value),26)
                    self.assertEqual(value[:12],'0'*12)
                    self.assertEqual(value[12:16],hardware)
                    self.assertEqual(value[16:],'0'*10)
                    self.assertNotIn('\0',value)

    def test_temporary_sn_is_derived_and_has_no_separate_configuration(self):
        config=StationConfig()
        other=replace(config,project='A2',subcategory='LFRR',factory='JS',production_date='261103')
        self.assertEqual(config.base_sn,other.base_sn)
        self.assertEqual(config.base_sn,replace(config,hardware_version='0222',model='222').base_sn.replace('0222','0221'))
        self.assertNotIn('base_sn',asdict(config))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'station.json'
            path.write_text(json.dumps(dict(base_sn=SN,hardware_version='0221')),encoding='utf-8')
            self.assertEqual(load_config(path).base_sn,temporary_sn('0221'))

    def test_hardware_dictionary_matches_0221_and_each_dropdown_digit(self):
        self.assertEqual(hardware_parts('0221'),dict(A='0',B='2',C='2',D='1'))
        self.assertEqual(hardware_parts('1323'),dict(A='1',B='3',C='2',D='3'))
        for invalid in ('221','02211','02!1','02 1'):
            with self.assertRaises(ValueError): hardware_parts(invalid)

    def test_revision_numbers_are_not_a_test_profile_whitelist(self):
        for value,torque in (('0112',30),('0222',30),('0132',50),('0142',50),('0223',30),('9229',30)):
            config=StationConfig(hardware_version=value,model=value[1:])
            config.validate()
            self.assertEqual(config.torque,torque)

    def test_known_hardware_without_test_profile_is_remembered_but_cannot_run(self):
        config=StationConfig(hardware_version='0151',model='151')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'station.json'
            save_config(config,path)
            self.assertEqual(load_config(path).hardware_version,'0151')
            self.assertFalse(config.test_profile_available)
            with self.assertRaisesRegex(ValueError,'产测参数'): config.validate(live=True)
            with self.assertRaisesRegex(ValueError,'产测参数'): _=config.torque

    def test_load_derives_legacy_model_from_single_hardware_field(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'station.json'
            path.write_text(json.dumps(dict(model='111',hardware_version='0221')),encoding='utf-8')
            self.assertEqual(load_config(path).model,'221')

    def test_production_defaults_use_real_devices_and_no_fixture_input(self):
        config=StationConfig()
        self.assertFalse(config.simulation)
        self.assertTrue(config.automatic)
        self.assertEqual((config.power_port,config.dyno_port),('auto','auto'))
        self.assertEqual(config.mes_url, 'https://www.mirrormetech.com/mes-api')
        self.assertEqual(config.report_upload_url, 'https://www.mirrormetech.com/mes-api/open-api/test-report')
        self.assertNotIn('fixtureId',mes_payload(config,'test-request'))
    def test_remembers_id_and_model_without_secrets_or_old_auto_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'station.json'
            config = StationConfig(target_id=3, automatic=False)
            save_config(config, path)
            loaded = load_config(path)
            self.assertEqual(asdict(loaded), asdict(config))
            self.assertEqual(loaded.target_id, 3)
            self.assertEqual(loaded.production_date, '')
            self.assertNotIn('api_key', json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])

    def test_live_preflight_generates_temporary_sn_and_requires_mes_configuration(self):
        with self.assertRaisesRegex(ValueError, 'MES'):
            StationConfig(mes_url='').validate(live=True)
        StationConfig().validate(live=True)
        StationConfig(mes_url='https://example.test').validate(live=True)
        for cfg in (StationConfig(target_id=128), StationConfig(hardware_version='0111'),
                    StationConfig(project='TOO-LONG')):
            with self.assertRaises(ValueError): cfg.validate()
        StationConfig(project='X2',category='ABCD',subcategory='XY12',factory='Z9').validate()


class MesTests(unittest.TestCase):
    def test_temporary_sn_cannot_be_accepted_as_a_formal_mes_sn(self):
        reply=self.reply()
        reply['data']['sns']=[temporary_sn('0221')]
        client=MesClient(StationConfig(mes_url='https://example.test'),Mock(return_value=reply))
        with patch.dict(os.environ,MIRRORME_MES_API_KEY='test-only-key'):
            with self.assertRaises(IntegrationError): client.allocate(self.payload())

    def payload(self):
        return mes_payload(StationConfig(production_date='261028'), 'same-uuid')

    def reply(self):
        return dict(code=200, data=dict(requestId='same-uuid', duplicated=True, count=1, sns=[SN]))

    @patch.dict(os.environ, MIRRORME_MES_API_KEY='test-secret')
    def test_retries_transport_and_only_transient_business_errors_with_same_id(self):
        http = Mock(side_effect=[TimeoutError(), dict(code=500,msg='流水号发号冲突，请重试'), self.reply()])
        pause = Mock()
        client = MesClient(StationConfig(mes_url='https://example.test/mes-api'), http, pause)
        payload = self.payload()
        self.assertEqual(client.allocate(payload), SN)
        self.assertEqual(http.call_count, 3)
        self.assertTrue(all(c.args[1] == payload for c in http.call_args_list))
        self.assertEqual(pause.call_count, 2)
        # 幂等命中没有 seqFrom / seqTo，也必须成功。

    @patch.dict(os.environ, MIRRORME_MES_API_KEY='test-secret')
    def test_http_200_business_auth_and_parameter_failures_do_not_retry(self):
        for code,msg in ((401,'密钥错误'),(500,'硬件版本未登记')):
            http = Mock(return_value=dict(code=code,msg=msg))
            with self.assertRaises(IntegrationError):
                MesClient(StationConfig(mes_url='https://example.test'),http).allocate(self.payload())
            self.assertEqual(http.call_count,1)

    @patch.dict(os.environ, MIRRORME_MES_API_KEY='test-secret')
    def test_mismatched_or_wrong_hardware_sn_rejected(self):
        for field,value in (('requestId','other'),('sns',[SN[:12]+'0111'+SN[16:]]),('count',2)):
            reply=self.reply();reply['data'][field]=value
            with self.assertRaises(IntegrationError):
                MesClient(StationConfig(mes_url='https://example.test'),Mock(return_value=reply)).allocate(self.payload())

    def test_date_and_fixture_are_validated(self):
        for config in (StationConfig(production_date='260231'), StationConfig(fixture_id='工位')):
            with self.assertRaises(ValueError): mes_payload(config,'valid-id')


class PrinterTests(unittest.TestCase):
    def test_top_level_success_does_not_override_failed_item(self):
        http=Mock(return_value=dict(success=True,data=dict(results=[dict(sn=SN,success=False,message='缺纸')])))
        with self.assertRaisesRegex(IntegrationError,'缺纸'):
            PrinterClient(StationConfig(),http).print_sn(SN)
        self.assertEqual(http.call_count,1)

    def test_unknown_outcome_never_auto_retries(self):
        for response in (TimeoutError(), dict(success=True,data=dict(results=[]))):
            http=Mock(side_effect=response) if isinstance(response,Exception) else Mock(return_value=response)
            with self.assertRaises(PrintUnknown): PrinterClient(StationConfig(),http).print_sn(SN)
            self.assertEqual(http.call_count,1)

    def test_print_request_does_not_include_wifi_and_respects_item(self):
        http=Mock(return_value=dict(success=True,data=dict(results=[dict(sn=SN,success=True)])))
        PrinterClient(StationConfig(),http).print_sn(SN)
        self.assertEqual(http.call_args.args[1],dict(items=[dict(sn=SN)],copies=1))
        self.assertGreaterEqual(http.call_args.kwargs['timeout'],3)


class MaintenanceTests(unittest.TestCase):
    def test_temporary_sn_wire_data_uses_ascii_zeroes_and_only_two_binary_pad_bytes(self):
        config=StationConfig()
        maintenance=Maintenance(config)
        bench=Mock()
        bench.motor.return_value.read_serial_bytes.return_value=b'\0'*28
        @contextmanager
        def connected(): yield bench
        maintenance.connect=connected
        maintenance.write=Mock()
        maintenance.wait_sn=Mock()
        maintenance.write_sn(config.base_sn,'未写入 SN')
        calls=maintenance.write.call_args_list
        self.assertEqual([call.args[2] for call in calls],list(range(0x2004,0x200B)))
        raw=b''.join(call.args[4].to_bytes(4,'little') for call in calls)
        self.assertEqual(raw[:26],b'0'*12+b'0221'+b'0'*10)
        self.assertEqual(raw[26:],b'\0\0')
        maintenance.wait_sn.assert_called_once_with(config.base_sn)

    def bench(self):
        transport=FakeTransport()
        return type('BenchFake',(),{'_bus':MotorBus(transport)})(),transport

    def test_write_sn_protocol_is_explicit_4_bytes_and_ack_not_usb_success(self):
        bench,transport=self.bench()
        transport.on_send=lambda f:[CanFrame(0x583,b'\x60'+f.data[1:4]+b'\0'*4)]
        Maintenance.write(bench,3,0x2004,4,int.from_bytes(b'C2KN','little'))
        self.assertEqual(transport.sent[0].can_id,0x603)
        self.assertEqual(transport.sent[0].data,b'\x23\x04\x20\x00C2KN')
        self.assertFalse(bench._bus._sdo_uncertain)

    def test_read_reply_width_is_checked(self):
        bench,transport=self.bench()
        transport.on_send=lambda f:[CanFrame(0x583,b'\x43'+f.data[1:4]+b'\1\0\0\0')]
        with self.assertRaisesRegex(SdoError,'长度'):
            Maintenance.read(bench,3,0x2072,2)
        self.assertTrue(bench._bus._sdo_uncertain)

    def test_late_ack_cannot_be_used_to_retry_write(self):
        bench,transport=self.bench()
        with self.assertRaises(TimeoutError): Maintenance.write(bench,3,0x2004,4,1,timeout=.001)
        with self.assertRaises(SdoError): Maintenance.write(bench,3,0x2004,4,1)
        self.assertEqual(len(transport.sent),1)

    def test_discovery_prefers_id_one_and_does_not_write(self):
        bench,transport=self.bench()
        def answer(frame):
            node=frame.can_id-0x600
            if node!=1: return []
            index=int.from_bytes(frame.data[1:3],'little')
            if index==0x2040:
                return [CanFrame(0x581,b'\x4f'+frame.data[1:4]+b'\1\0\0\0')]
            offset=(index-0x2004)*4
            raw=SN.encode()+b'\0\0'
            return [CanFrame(0x581,b'\x43'+frame.data[1:4]+raw[offset:offset+4])]
        transport.on_send=answer
        maintenance=Maintenance(StationConfig())
        @contextmanager
        def connected(): yield bench
        maintenance.connect=connected
        self.assertEqual(maintenance.discover(),dict(node_id=1,sn=SN))
        self.assertTrue(all(frame.data[0]==0x40 for frame in transport.sent))

    def test_multiple_known_ids_block_all_maintenance(self):
        bench,transport=self.bench()
        transport.on_send=lambda frame:[CanFrame(0x580+frame.can_id-0x600,
            b'\x4f'+frame.data[1:4]+bytes([frame.can_id-0x600,0,0,0]))]
        maintenance=Maintenance(StationConfig())
        @contextmanager
        def connected(): yield bench
        maintenance.connect=connected
        with self.assertRaisesRegex(RuntimeError,'多个'): maintenance.discover()
        self.assertTrue(all(frame.data[0]==0x40 for frame in transport.sent))

    def test_exhaustive_prewrite_scan_finds_extra_nonpreferred_motor(self):
        bench,_=self.bench()
        maintenance=Maintenance(StationConfig())
        @contextmanager
        def connected(): yield bench
        maintenance.connect=connected
        def read(_bench,node,*args,**kwargs):
            if node in (1,7): return node
            raise TimeoutError()
        maintenance.read=Mock(side_effect=read)
        with self.assertRaisesRegex(RuntimeError,'多个'): maintenance.discover(exhaustive=True)
        self.assertEqual(maintenance.read.call_count,127)

    def test_assign_id_uses_platform_specific_reboot_and_verifies_target(self):
        for model in ('221','111'):
            maintenance=Maintenance(StationConfig(model=model,hardware_version='0'+model))
            bench=Mock();bench.motor.return_value.read_serial_bytes.return_value=SN.encode()+b'\0\0'
            @contextmanager
            def connected(node):
                self.assertEqual(node,1)
                yield bench
            maintenance.connect=connected
            maintenance.write=Mock()
            maintenance.wait_sn=Mock()
            maintenance.assign_id(1,SN)
            calls=[call.args[1:] for call in maintenance.write.call_args_list]
            self.assertEqual(calls[0],(1,0x2040,1,3))
            self.assertEqual(len(calls),1 if model=='221' else 2)
            if model=='111':
                self.assertEqual(calls[1],(1,0x200A,4,int.from_bytes(SN.encode()[24:]+b'\0\0','little')))
            maintenance.wait_sn.assert_called_once_with(SN)

    def test_adm_id_reset_lost_ack_requires_readback_instead_of_rewriting(self):
        maintenance=Maintenance(StationConfig())
        bench=Mock();bench.motor.return_value.read_serial_bytes.return_value=SN.encode()+b'\0\0'
        @contextmanager
        def connected(node): yield bench
        maintenance.connect=connected
        maintenance.write=Mock(side_effect=TimeoutError())
        maintenance.wait_sn=Mock()
        maintenance.assign_id(1,SN)
        self.assertEqual(maintenance.write.call_count,1)
        maintenance.wait_sn.assert_called_once_with(SN)

    def calibration_fixture(self, reads, model='221'):
        config=StationConfig(model=model,hardware_version='0'+model)
        maintenance=Maintenance(config)
        motor=Mock()
        bench=Mock()
        bench.motor.return_value=motor
        @contextmanager
        def connected(): yield bench
        maintenance.connect=connected
        maintenance.read=Mock(side_effect=reads)
        maintenance.write=Mock()
        maintenance.wait=Mock()
        return maintenance,motor,bench

    def test_adm_completion_requires_reset_flag_and_new_completion_not_only_ack(self):
        # 初始无错、确认标志已清、进行中、完成两次，最后才失能放行。
        maintenance,motor,_=self.calibration_fixture([0,0,0,0,0,1,0,1])
        maintenance.calibrate()
        self.assertEqual([call.args[2:5] for call in maintenance.write.call_args_list],
                         [(0x2072,2,0),(0x2002,2,0xF1)])
        self.assertEqual(maintenance.read.call_count,8)
        self.assertEqual(motor.disable.call_count,2)

    def test_old_gd_calibration_does_not_prove_this_calibration(self):
        maintenance,_,_=self.calibration_fixture([0],model='111')
        with self.assertRaisesRegex(RuntimeError,'本次校准进度'): maintenance.calibrate()
        maintenance.write.assert_not_called()

    def test_calibration_fault_stops_without_pretending_disable_ack_is_cancellation(self):
        maintenance,_,bench=self.calibration_fixture([0,0,0x0400])
        with self.assertRaisesRegex(RuntimeError,'0x0400') as failure:
            maintenance.calibrate()
        self.assertTrue(any('切断工装电源' in note for note in failure.exception.__notes__))
        bench._bus.transport.send.assert_called_once()


class PipelineTests(unittest.TestCase):
    def runner(self, root, service=None, cancelled=lambda:False):
        cfg=StationConfig(simulation=True)
        service=service or DemoServices(cfg,cancelled,lambda *_:None)
        # 真正报告渲染另有端到端演示；状态机单测不重复渲染图片。
        service.render=Mock()
        return StationRunner(cfg,root=root,services=service,cancelled=cancelled),service

    def test_full_demo_has_no_hardware_or_http_and_all_steps_persisted(self):
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.factory_test.maintenance.Canable',side_effect=AssertionError('USB')), \
             patch('scripts.factory_test.integrations.json_http',side_effect=AssertionError('HTTP')):
            runner,service=self.runner(tmp)
            result=runner.begin('blank')
            self.assertEqual(result['status'],'COMPLETE')
            self.assertTrue(result['demo'])
            self.assertTrue(result['module_sn'].startswith('DEMO'))
            saved=json.loads((Path(result['directory'])/'station.json').read_text(encoding='utf-8'))
            self.assertTrue(all(s=='DONE' for s in saved['steps'].values()))

    def test_workpiece_date_is_frozen_before_measurement_and_mes_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            def allocate(payload):
                self.assertEqual(payload['productionDate'],runner.record['production_date'])
                return 'DEMO000000000221'+payload['productionDate']+'0001'
            service.allocate=allocate
            with patch('scripts.factory_test.integrations.datetime',wraps=datetime) as later:
                later.now.return_value=datetime(2030,1,2)
                result=runner.begin('blank')
            self.assertEqual(result['status'],'COMPLETE')
            self.assertEqual(result['base_sn'][16:],'0'*10)
            self.assertEqual(result['production_date'],result['mes_payload']['productionDate'])
            self.assertNotEqual(result['mes_payload']['productionDate'],'300102')

    def test_temporary_sn_is_written_before_formal_sn_and_only_formal_sn_is_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            service.write_sn=Mock()
            service.allocate=Mock(return_value=SN)
            service.print_sn=Mock(return_value=dict(success=True))
            result=runner.begin('incoming',1)
            self.assertEqual(result['status'],'COMPLETE')
            self.assertEqual(service.write_sn.call_args_list[0].args,(runner.config.base_sn,'incoming'))
            self.assertEqual(service.write_sn.call_args_list[1].args,(SN,runner.config.base_sn))
            service.print_sn.assert_called_once_with(SN)

    def test_incoming_id_is_separate_from_persistent_target_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            service.assign_id=Mock()
            result=runner.begin('blank',detected_id=1)
            self.assertEqual(result['status'],'COMPLETE')
            service.assign_id.assert_called_once_with(1,'blank')
            self.assertEqual(result['detected_id'],1)
            self.assertEqual(result['config']['target_id'],3)

    def test_calibration_failure_prevents_measurement_and_allocating(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            service.calibrate=Mock(side_effect=RuntimeError('calibration fault'))
            service.measure=Mock();service.allocate=Mock();service.print_sn=Mock()
            result=runner.begin('blank')
            self.assertEqual(result['status'],'FAILED')
            service.measure.assert_not_called();service.allocate.assert_not_called();service.print_sn.assert_not_called()
            with self.assertRaises(RecoveryRequired): runner.begin('another')

    def test_test_failure_never_allocates_or_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            service.measure=Mock(return_value={'status':'FAIL'})
            service.allocate=Mock();service.print_sn=Mock()
            self.assertEqual(runner.begin('blank')['status'],'FAILED')
            service.allocate.assert_not_called();service.print_sn.assert_not_called()

    def test_allocation_recovery_uses_persisted_payload_and_no_retesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            service.allocate=Mock(side_effect=TimeoutError('MES unreachable'))
            first=runner.begin('blank')
            self.assertEqual(first['steps']['allocate'],'FAILED')
            original=copy.deepcopy(first['mes_payload'])
            service.allocate=Mock(return_value='DEMO0000000002212610280001')
            service.calibrate=Mock();service.measure=Mock()
            result=runner.resume()
            self.assertEqual(result['status'],'COMPLETE')
            service.allocate.assert_called_once_with(original)
            service.measure.assert_not_called();service.calibrate.assert_not_called()

    def test_print_unknown_survives_restart_and_blocks_blind_reprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            service.print_sn=Mock(side_effect=PrintUnknown('timeout'))
            result=runner.begin('blank')
            self.assertIn('print',result['steps'],msg=str(result))
            self.assertEqual(result['steps']['print'],'UNKNOWN')
            restarted,_=self.runner(tmp)
            with self.assertRaisesRegex(RecoveryRequired,'人工核对'): restarted.resume()
            self.assertEqual(restarted.pending()['module_sn'],result['module_sn'])

    def test_upload_configuration_pending_does_not_turn_passed_motor_into_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            service.upload=Mock(return_value=dict(status='BLOCKED_CONFIGURATION'))
            result=runner.begin('blank')
            self.assertEqual(result['status'],'COMPLETE')
            self.assertEqual(result['steps']['report'],'DONE')
            self.assertEqual(result['steps']['upload'],'DONE')
            self.assertEqual(result['report_delivery']['status'],'BLOCKED_CONFIGURATION')
            self.assertEqual(result['summary']['status'],'PASS')

    def test_operator_can_archive_failed_piece_without_deleting_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp)
            service.print_sn=Mock(side_effect=PrintUnknown('unknown'))
            failed=runner.begin('blank')
            sn=failed['module_sn']
            closed=runner.close_pending('已人工核对标签并隔离工件')
            self.assertEqual(closed['status'],'CLOSED')
            self.assertEqual(closed['module_sn'],sn)
            self.assertIsNone(runner.pending())
            self.assertTrue((Path(closed['directory'])/'station.json').exists())

    def test_cancel_before_actions_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,service=self.runner(tmp,cancelled=lambda:True)
            service.write_sn=Mock()
            result=runner.begin('blank')
            self.assertEqual(result['status'],'FAILED')
            service.write_sn.assert_not_called()

    def test_cancel_during_step_notification_prevents_the_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopped=[False]
            runner,service=self.runner(tmp,cancelled=lambda:stopped[0])
            service.assign_id=Mock()
            def emit(kind,value):
                if kind=='step' and value['status']=='RUNNING': stopped[0]=True
            runner.emit=emit
            self.assertEqual(runner.begin('blank')['status'],'FAILED')
            service.assign_id.assert_not_called()

    def test_presence_requires_removal_and_two_successful_probes(self):
        latch=PresenceLatch(2)
        self.assertFalse(latch.observe(True,0));self.assertTrue(latch.observe(True,1))
        latch.completed()
        self.assertFalse(latch.observe(True,2))
        self.assertFalse(latch.observe(False,3))
        self.assertFalse(latch.observe(True,4))  # 短抖动不重新启动。
        self.assertTrue(latch.wait_removal)
        latch.observe(False,5);latch.observe(False,7)
        self.assertFalse(latch.observe(True,8));self.assertTrue(latch.observe(True,9))
