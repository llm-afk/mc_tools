import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.feedforward_sweep import app


class FeedforwardSweepTests(unittest.TestCase):
    def test_preview_has_zero_gains_and_never_opens_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'preview.csv'
            with patch.object(app,'Canable',side_effect=AssertionError('hardware')), \
                 patch('sys.stdout',new_callable=io.StringIO):
                result=app.main(['--preview','--amplitude','.1','--duration','.1',
                                 '--rate','100','--output',str(path)])
            self.assertEqual(result,0)
            with path.open(encoding='utf-8-sig',newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(tuple(rows[0]),app.COLUMNS)
            self.assertTrue(all(r['kp']=='0' and r['kd']=='0' for r in rows))
            self.assertTrue(all(r['position_rad']=='' for r in rows))
            targets=[float(r['target_feedforward_torque']) for r in rows]
            self.assertEqual((targets[0],targets[-1]),(0,0))
            self.assertGreater(max(targets),0)
            self.assertLess(min(targets),0)
            self.assertLessEqual(max(map(abs,targets)),.100001)

    def test_source_defaults_and_positive_amplitude_validation(self):
        defaults=app.parse_args([])
        self.assertEqual(defaults.amplitude,app.AMPLITUDE)
        self.assertEqual((defaults.kp,defaults.kd),(0,0))
        for argv in (['--amplitude','0'],['--amplitude','nan']):
            with self.subTest(argv=argv), patch('sys.stderr',new_callable=io.StringIO):
                with self.assertRaises(SystemExit): app.parse_args(argv)
        args=app.parse_args(['--amplitude','.2'])
        self.assertEqual((args.kp,args.kd,args.feedforward),(0,0,True))
