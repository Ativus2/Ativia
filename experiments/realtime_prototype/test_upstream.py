import unittest
from experiments.realtime_prototype.vendor.sentence_divider import SentenceDivider


class UpstreamSentenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_clause_is_emitted_before_upstream_eof(self):
        reached_end=False
        async def tokens():
            nonlocal reached_end
            yield '你好，'
            yield '今天过得怎么样？'
            reached_end=True
        stream=SentenceDivider().process_stream(tokens())
        first=await anext(stream)
        self.assertEqual(first.text,'你好，'); self.assertFalse(reached_end)
        rest=[s.text async for s in stream]
        self.assertEqual(''.join([first.text]+rest),'你好，今天过得怎么样？')

    async def test_text_without_terminal_punctuation_is_not_lost(self):
        async def tokens():
            yield '我在'; yield '这里'
        self.assertEqual(''.join([s.text async for s in SentenceDivider().process_stream(tokens())]),'我在这里')

    async def test_think_tags_are_exposed_for_filtering(self):
        async def tokens(): yield '<think>内部文本。</think>公开回答。'
        output=[s async for s in SentenceDivider().process_stream(tokens())]
        visible=''.join(s.text for s in output if not any(tag.name=='think' for tag in s.tags))
        self.assertEqual(visible,'公开回答。')


if __name__=='__main__': unittest.main()
