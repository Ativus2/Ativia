import unittest
from pathlib import Path
from tools.realtime_benchmark.common import Metrics, summary, read_fixture, REPORTS
from speech_activity import SpeechActivity
from runtime_settings import RuntimeSettings


class BenchmarkContractTests(unittest.TestCase):
    def test_failed_and_silent_rounds_remain_in_denominator(self):
        rows=[]
        for i in range(20):
            metrics=Metrics('A',i); metrics.mark('speech_end',at=1); metrics.mark('text_ready',at=2)
            if i<19:
                metrics.mark('audible',at=3+i); metrics.row['status']='complete'
            else: metrics.row['status']='failed'
            rows.append(metrics.finalize())
        result=summary(rows)['A']
        self.assertEqual((result['rounds'],result['audible'],result['failed'],result['silent_replies']),(20,19,1,1))
        self.assertEqual(result['p95_first_audible_ms'],20000)

    def test_explicit_interruption_is_not_a_silent_reply(self):
        metrics=Metrics('B',1); metrics.mark('text_ready'); metrics.row['status']='cancelled'
        self.assertFalse(metrics.finalize()['silent_reply'])

    def test_all_twenty_inputs_are_accepted_once_by_current_vad(self):
        paths=sorted((REPORTS/'fixtures').glob('*.wav')); self.assertEqual(len(paths),20)
        for path in paths:
            samples,end=read_fixture(path); vad=SpeechActivity(RuntimeSettings()); found=[]
            for i in range(0,len(samples),320): found+=vad.feed(samples[i:i+320],(i+320)/16000)
            self.assertEqual(len(found),1,path.name)
            self.assertTrue(.5 < found[0].detected_at-end < .7,path.name)


if __name__=='__main__': unittest.main()
