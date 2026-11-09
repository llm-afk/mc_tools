import unittest
from unittest.mock import Mock,patch
from collections import deque

from scripts.factory_test.waveform import WaveformPlot,envelope,tick_text,FRAME_INTERVAL
from scripts.factory_test.manual_control import ControlPage


class Canvas:
    def __init__(self): self.items={}
    def create_line(self,*coords,**options): return self.create('line',coords,options)
    def create_text(self,*coords,**options): return self.create('text',coords,options)
    def create(self,kind,coords,options):
        item=len(self.items)+1
        self.items[item]=dict(kind=kind,coords=coords,**options)
        return item
    def coords(self,item,*coords): self.items[item]['coords']=coords
    def itemconfigure(self,item,**options): self.items[item].update(options)


def sample(at,value=0,target=0):
    return dict(at=at,position=value,velocity=value,torque=value,
                target=dict(position=target,velocity=target,torque=target))


class WaveformTests(unittest.TestCase):
    def test_frames_resize_and_window_changes_reuse_every_canvas_item(self):
        canvas=Canvas(); plot=WaveformPlot(canvas)
        original=set(canvas.items)
        for span,width,height in ((5,760,860),(5,760,860),(30,900,700),(10,600,640)):
            plot.render([sample(1),sample(2,1)],span,width,height)
            self.assertEqual(set(canvas.items),original)

    def test_time_ticks_stay_fixed_and_concise_as_data_scrolls(self):
        canvas=Canvas(); plot=WaveformPlot(canvas)
        for at in (20.867235,100.001,999.32189):
            plot.render([sample(at-1),sample(at)],5,760,860)
            for row in plot.rows:
                self.assertEqual([canvas.items[i]['text'] for i in row['xlabels']],
                                 ['0.0','1.0','2.0','3.0','4.0','5.0'])
                self.assertEqual(canvas.items[row['time']]['text'],'时间 / s')
        plot.render([sample(1),sample(2)],.5,760,860)
        self.assertEqual([canvas.items[i]['text'] for i in plot.rows[0]['xlabels']],
                         ['0.0','0.1','0.2','0.3','0.4','0.5'])

    def test_target_and_feedback_are_solid_and_both_visible_at_overlap(self):
        canvas=Canvas(); plot=WaveformPlot(canvas)
        plot.render([sample(1),sample(2)],5,760,860)
        for row in plot.rows:
            target,feedback=(canvas.items[row[name]] for name in ('target','feedback'))
            self.assertNotIn('dash',target); self.assertNotIn('dash',feedback)
            self.assertNotEqual(target['fill'],feedback['fill'])
            self.assertGreater(target['width'],feedback['width'])
            self.assertEqual(target['state'],'normal'); self.assertEqual(feedback['state'],'normal')

    def test_target_changes_are_drawn_at_the_change_time(self):
        canvas=Canvas(); plot=WaveformPlot(canvas)
        plot.render([sample(0,target=0),sample(1,target=0),sample(2,target=1)],5,760,860)
        coords=canvas.items[plot.rows[0]['target']]['coords']
        vertices=list(zip(coords[::2],coords[1::2]))
        self.assertTrue(any(a[0]==b[0] and a[1]!=b[1] for a,b in zip(vertices,vertices[1:])))

    def test_pixel_envelope_preserves_short_peaks_and_time_order(self):
        points=[sample(i*.001) for i in range(10001)]
        points[2501]['position']=7
        points[2502]['position']=-5
        reduced=envelope(points,'position',False,0,10,100)
        self.assertLessEqual(len(reduced),204)
        self.assertEqual((max(v for _,v in reduced),min(v for _,v in reduced)),(7,-5))
        self.assertEqual(reduced[0],(0,0)); self.assertEqual(reduced[-1],(10,0))
        self.assertEqual(reduced,sorted(reduced))

    def test_small_noise_keeps_vertical_scale_and_large_peak_expands_it(self):
        canvas=Canvas(); plot=WaveformPlot(canvas)
        plot.render([sample(1,1),sample(2,1.001)],5,760,860)
        old=plot.bounds.copy()
        plot.render([sample(3,1.002),sample(4,1.003)],5,760,860)
        self.assertEqual(old,plot.bounds)
        plot.render([sample(5,10),sample(6,1)],5,760,860)
        self.assertGreaterEqual(plot.bounds['position'][1],10)

    def test_zoom_in_waits_and_reset_clears_old_connection_scale(self):
        canvas=Canvas(); plot=WaveformPlot(canvas)
        with patch('scripts.factory_test.waveform.time.monotonic',return_value=0):
            plot.render([sample(1,-10),sample(2,10)],5,760,860)
        old=plot.bounds['position']
        with patch('scripts.factory_test.waveform.time.monotonic',return_value=.5):
            plot.render([sample(9,0),sample(10,.01)],5,760,860)
        self.assertEqual(old,plot.bounds['position'])
        with patch('scripts.factory_test.waveform.time.monotonic',return_value=1.6):
            plot.render([sample(9,0),sample(10,.01)],5,760,860)
        self.assertLess(plot.bounds['position'][1]-plot.bounds['position'][0],old[1]-old[0])
        plot.reset(); self.assertFalse(plot.bounds)

    def test_decimal_tick_format_does_not_emit_negative_zero_or_long_fractions(self):
        self.assertEqual(tick_text(-1e-9,1),'0.0')
        self.assertEqual(tick_text(62.867235,1),'62.9')
        self.assertEqual(tick_text(.125,.025),'0.125')
        self.assertEqual(tick_text(.25,.25),'0.25')
        self.assertLessEqual(FRAME_INTERVAL,1/60)

    def test_unchanged_status_does_not_repeat_tk_writes_or_discard_samples(self):
        page=ControlPage.__new__(ControlPage)
        page.session_generation=1;page.samples=deque();page._status_text={};page.record_writer=None
        page.status_values={name:Mock() for name in ('motor','motor_temperature','board_temperature')}
        data=dict(sample(1),enabled=False,motor_temperature=25,board_temperature=26)
        page.event('control_sample',data)
        page.event('control_sample',dict(data,at=2))
        for variable in page.status_values.values(): variable.set.assert_called_once()
        self.assertEqual(len(page.samples),2)

    def test_drawing_has_one_independent_timer_and_keeps_running_after_a_display_error(self):
        page=ControlPage.__new__(ControlPage)
        page.host=Mock(page='control');page.frame=Mock();page.plot_dirty=True;page.frozen=False
        page.draw=Mock(side_effect=ValueError('display error'))
        with patch('scripts.factory_test.manual_control.time.perf_counter',return_value=0):
            page.refresh_plot()
        page.host.callback_exception.assert_called_once()
        page.frame.after.assert_called_once_with(17,page.refresh_plot)
        page.frame.after.reset_mock();page.draw.reset_mock();page.frozen=True
        with patch('scripts.factory_test.manual_control.time.perf_counter',return_value=0):
            page.refresh_plot()
        page.draw.assert_not_called()
        page.frame.after.assert_called_once_with(17,page.refresh_plot)

    def test_frame_timer_accounts_for_render_work_and_yields_when_over_budget(self):
        for elapsed,delay in ((.01,7),(.1,1)):
            page=ControlPage.__new__(ControlPage)
            page.host=Mock(page='control');page.frame=Mock();page.plot_dirty=True;page.frozen=False
            page.draw=Mock()
            with patch('scripts.factory_test.manual_control.time.perf_counter',side_effect=[1,1+elapsed]):
                page.refresh_plot()
            page.frame.after.assert_called_once_with(delay,page.refresh_plot)


if __name__=='__main__': unittest.main()
