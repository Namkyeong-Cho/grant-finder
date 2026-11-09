"""An opportunity must be read successfully for this exact CV profile."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grantfinder.agent import answer_question


class Client:
    model = 'synthetic-local-model'
    def __init__(self, identifier):
        self.identifier, self.step = identifier, 0
    def chat(self, messages, tools=None):
        self.step += 1
        if self.step == 1:
            message = {'role':'assistant','content':'','tool_calls':[{'function':{
                'name':'read_funding_candidate','arguments':{'candidate_id':self.identifier}}}]}
        else:
            message = {'role':'assistant','content':'원문을 읽은 지원 후보 답변입니다.'}
        return {'message':message}


class CatalogAgentTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'id':'synthetic-professor'}
        self.catalog = {'profile_id':'synthetic-professor','candidates':[
            {'id':'synthetic-opportunity','title':'가상 지원사업','source_url':'https://example.org/call',
             'route':'host_required','availability':'announced','personal_constraints':['해외 호스트 확인 필요']}]}
        self.now = datetime(2026,11,6,tzinfo=ZoneInfo('Asia/Seoul'))
    def answer(self, identifier, catalog=None):
        return answer_question(Client(identifier),'이 후보의 조건을 알려주세요.',[],[],self.profile,
                               {'completed_tasks':[]},self.now,catalog or self.catalog)
    def test_successful_catalog_read_counts_as_evidence(self):
        result = self.answer('synthetic-opportunity')
        self.assertEqual(result['candidate_ids'],['synthetic-opportunity'])
        self.assertEqual(result['notice_ids'],[])
        self.assertEqual(result['tool_calls'][0]['result']['personal_constraints'],['해외 호스트 확인 필요'])
    def test_missing_candidate_does_not_permit_a_final_answer(self):
        with self.assertRaises(RuntimeError):
            self.answer('nonexistent-opportunity')
    def test_another_profiles_saved_catalog_is_not_used(self):
        with self.assertRaises(RuntimeError):
            self.answer('synthetic-opportunity',dict(self.catalog,profile_id='different-person'))


if __name__ == '__main__':
    unittest.main()
