import csv
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from scripts.factory_test import app, report
from scripts.factory_test.preview import create_demo
from motor_platform import MotorBus, SdoError, CanFrame
from tests.test_motor import FakeTransport


class SerialTests(unittest.TestCase):
    def test_read_seven_objects_in_wire_order_without_enable(self):
        raw = b'C1SN0000000011010000000001'+b'\x00\x00'
        self.assertEqual(len(raw),28)
        transport=FakeTransport()
        def answer(frame):
            self.assertEqual(frame.can_id,0x601)
            self.assertEqual(frame.data[0],0x40)
            index=int.from_bytes(frame.data[1:3],'little')
            self.assertIn(index,range(0x2004,0x200B))
            offset=(index-0x2004)*4
            return [CanFrame(0x581,b'\x43'+frame.data[1:4]+raw[offset:offset+4])]
        transport.on_send=answer
        motor=MotorBus(transport).motor(1)
        self.assertEqual(motor.read_serial_number(),raw[:26].decode())
        self.assertEqual(len(transport.sent),7)
        self.assertIsNone(motor.enabled)

    def test_rejected_and_unprogrammed_sn(self):
        for cs in (0x80,0x4B,0x43):
            transport=FakeTransport()
            transport.on_send=lambda f:[CanFrame(0x581,bytes([cs])+f.data[1:4]+b'\0'*4)]
            with self.assertRaises(SdoError): MotorBus(transport).motor(1).read_serial_number()


class FactoryTests(unittest.TestCase):

    def test_linearity_uses_recorded_target_and_full_scale(self):
        rows=[dict(time_ms=str(i), motor_torque='999',
                   motor_application_target_torque=str(i), dyno_torque_smooth_nm=str(i*1.06))
              for i in range(11)]
        summary={'target_torque':10,'stages':{'kt':{}}}
        points,estimated=report.kt_target_curve(rows,summary)
        self.assertFalse(estimated)
        self.assertEqual(points[-1][0],10)
        deviation=max(abs(x-y) for x,y in points)/10*100
        self.assertAlmostEqual(deviation,6)
        self.assertEqual(report.metric('error',deviation,hi=5)['status'],'FAIL')
        self.assertEqual(report.metric('error',5,hi=5)['status'],'PASS')


    def args(self): return NS(id=1,serial='adapter',torque=30)

    def stages(self):
        return tuple((name,NS(main=Mock(return_value=result)),file) for name,result,file in (
            ('kt',{'reason':'completed'},'kt_curve.csv'),
            ('tn',{'reason':'stall_confirmed'},'curve.csv')))

    def test_sequential_reuse_and_sn_identity(self):
        stages=self.stages();identity=Mock(return_value='motorSN')
        with patch('builtins.print') as printed:
            result=app.execute(self.args(),Path('output'),stages=stages,identify=identity,pause=lambda _:None)
        self.assertEqual(printed.call_args_list[0].args[0], '电机 SN: motorSN')
        self.assertEqual(printed.call_args_list[1].args[0], '[1/2] KT')
        self.assertEqual(identity.call_count,3)
        for name,module,_ in stages:
            module.main.assert_called_once_with(
                ['--id', '1', '--serial', 'adapter', '--torque', '30'],
                output_dir=Path('output') / name,
                quiet=True,
            )
            self.assertEqual(result['stages'][name]['status'],'COMPLETED')

    def test_fault_or_sn_change_prevents_next_test(self):
        for mismatch in (False,True):
            stages=self.stages()
            if not mismatch: stages[0][1].main.side_effect=RuntimeError('CAN fault')
            identity=Mock(side_effect=['first','other'] if mismatch else ['same']*3)
            result=app.execute(self.args(),Path('output'),stages=stages,identify=identity,pause=lambda _:None)
            self.assertEqual(result['stages']['kt']['status'],'ERROR')
            stages[1][1].main.assert_not_called()

    def test_demo_metrics_and_unconfigured_standards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);summary=create_demo(root)
            sections=report.analyze(root,summary)
            self.assertEqual(report.overall(sections,summary['stages']),'PASS')
            self.assertAlmostEqual(sections[0]['fit'][0],.976,places=3)
            self.assertEqual(sections[1]['title'], '电机特性曲线')
            self.assertEqual(sections[1]['status'], 'PASS')
            self.assertEqual(len(sections[1]['metrics']), 3)
            summary['limits']={k:None for k in summary['limits']}
            sections=report.analyze(root,summary)
            self.assertEqual(report.overall(sections,summary['stages']),'PENDING')
            (root/'tn'/'curve.csv').unlink()
            sections=report.analyze(root,summary)
            self.assertEqual(report.overall(sections,summary['stages']),'INCOMPLETE')

    def test_zero_output_is_linearity_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);summary=create_demo(root)
            path=root/'kt'/'kt_curve.csv'
            with path.open(encoding='utf-8',newline='') as f:
                reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
            for row in rows: row['dyno_torque_smooth_nm']='0'
            with path.open('w',encoding='utf-8',newline='') as f:
                writer=csv.DictWriter(f,fields);writer.writeheader();writer.writerows(rows)
            section=report.analyze(root,summary)[0]
            self.assertEqual(section['status'],'FAIL')
            self.assertEqual(section['metrics'][0]['value'],100)

    def test_kt_pairing_uses_each_stream_time_not_row_index(self):
        rows=[dict(motor_time_ms=str(i*10),motor_torque=str(i),dyno_time_ms=str((9-i)*10),
                   dyno_torque_nm=str(2*(9-i))) for i in range(10)]
        slope,_,r2=report.regression(report.pair_kt(rows))
        self.assertAlmostEqual(slope,2)
        self.assertAlmostEqual(r2,1)


if __name__ == '__main__': unittest.main()
