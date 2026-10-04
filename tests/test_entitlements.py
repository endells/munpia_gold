"""Ownership and rental tests using only synthetic responses."""
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_gold import core as c


def result(data):
    return {'code': 'M000_00000', 'result': data}


class EntitlementTests(unittest.TestCase):
    def test_app_only_error_is_not_a_cookie_error(self):
        for code in ('A002_21014', 'A002_21015'):
            error = c.response_error({'code': code}, 400)
            self.assertNotIsInstance(error, c.LoginRequired)
            self.assertIn('앱 전용', str(error))
            self.assertIn(code, str(error))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = 1000.0
        self.logged_in = True
        self.calls = []
        self.items = [
            {'id':10,'num':1,'free':True},
            {'id':20,'num':2,'free':False,'purchased':True},
            {'id':30,'num':3,'free':False,'purchased':False,'rented':True,'remainRentSec':60},
            {'id':40,'num':4,'free':False,'purchased':False,'rented':False,'remainRentSec':0},
            {'id':50,'num':5,'free':False,'purchased':False,'rented':True,'remainRentSec':0},
            {'id':60,'num':6,'free':False,'purchased':'true'},
            {'id':70,'num':7,'free':False,'rented':True,'remainRentSec':'120'},
        ]
        self.conf = {'cookie':'session=synthetic','download_path':str(self.root/'books'),
                     'request_delay':1,'max_per_title':100,'make_epub':False,'include_author_comment':False}

    def tearDown(self):
        self.tmp.cleanup()

    def transport(self, path, params):
        self.calls.append(path)
        if path == '/api/member/my-info-simple':
            return {'login':self.logged_in}
        if path.endswith('/chapters'):
            return result({'list':[dict(x,novelId=1,title='합성 회차') for x in self.items], 'next':False,'total':len(self.items)})
        if '/entries/' in path:
            return result({'entry':{'id':int(path.split('/')[-1]),'content':'직접 작성한 합성 본문','attachments':[]}})
        return result({'novelInfo':{'id':1,'title':'합성 작품','authorName':'합성 작가'}})

    def engine(self, transport=None):
        return c.Engine(self.root/'history.db',client_factory=lambda **kw:c.Client(transport=transport or self.transport,**kw))

    def run_engine(self, engine, selected=None, kind='download'):
        engine.start(kind,['1'],self.conf,selected)
        engine.thread.join(5)
        self.assertFalse(engine.thread.is_alive())
        return engine.snapshot()

    def requested_ids(self):
        return [p.split('/')[-1] for p in self.calls if '/entries/' in p]

    def test_only_free_purchased_and_active_rent_are_downloaded(self):
        state = self.run_engine(self.engine())
        self.assertEqual(state['completed'],3)
        self.assertEqual(self.requested_ids(),['10','20','30'])
        self.assertEqual(state['skipped'],4)

    def test_selected_ids_cannot_grant_unpurchased_or_expired_access(self):
        self.run_engine(self.engine(),selected=['20','30','40','50','60','70'])
        self.assertEqual(self.requested_ids(),['20','30'])

    def test_analysis_has_purchase_and_rental_labels(self):
        state=self.run_engine(self.engine(),kind='analyze')
        eps={e['id']:e for e in state['analysis']['episodes']}
        self.assertEqual(eps['20']['access'],'purchased')
        self.assertEqual(eps['30']['access'],'rented')
        self.assertEqual(eps['50']['access'],'expired')
        self.assertFalse(eps['40']['available'])
        self.assertEqual(self.requested_ids(),[])

    def test_login_is_required_before_listing_or_downloading(self):
        self.logged_in=False
        state=self.run_engine(self.engine())
        self.assertEqual(state['status'],'auth_required')
        self.assertEqual(self.calls,['/api/member/my-info-simple'])
        self.calls.clear();self.conf['cookie']=''
        self.assertEqual(self.run_engine(self.engine())['status'],'auth_required')
        self.assertEqual(self.calls,[])

    def test_access_is_revalidated_when_rental_expires_in_queue(self):
        self.items = [self.items[1],dict(self.items[2],remainRentSec=1)]
        def transport(path, params):
            data=self.transport(path,params)
            if path.endswith('/entries/20'):
                self.clock+=2
            return data
        with patch.object(c.time,'monotonic',side_effect=lambda:self.clock):
            state=self.run_engine(self.engine(transport))
        self.assertEqual(self.requested_ids(),['20'])
        self.assertEqual(state['skipped'],1)

    def test_access_is_revalidated_after_request_throttle(self):
        def wait(seconds):
            self.clock+=seconds
            return False
        stop=types.SimpleNamespace(is_set=lambda:False,wait=wait)
        client=c.Client(stop=stop,delay=2,cookie='session=synthetic')
        client.last_request=self.clock
        client.access_grants[('1','30')]={'rented':True,'remainRentSec':1,'_access_checked_at':self.clock}
        with patch.object(c.time,'monotonic',side_effect=lambda:self.clock), patch.object(client.opener,'open') as opened:
            with self.assertRaises(c.AccessDenied):client.entry('1','30')
            opened.assert_not_called()

    def test_direct_unverified_entry_and_payment_endpoints_never_requested(self):
        client=c.Client(transport=self.transport,cookie='session=synthetic')
        with self.assertRaises(c.AccessDenied):client.entry('1','20')
        for path in ['/api/v1/paid/purchase','/api/v-1-2/paid/purchase','/api/v1/paid/order/selective/novels/1/entries']:
            with self.assertRaises(c.MunpiaError):client.get(path)
        self.assertEqual(self.calls,[])

    def test_refreshed_chapters_revoke_old_grants(self):
        client=c.Client(transport=self.transport,cookie='session=synthetic')
        client.chapters('1');client.entry('1','20')
        self.items[1]['purchased']=False
        client.chapters('1')
        with self.assertRaises(c.AccessDenied):client.entry('1','20')
        self.assertEqual(self.requested_ids(),['20'])

    def test_nonfinite_negative_and_boolean_rent_values_are_not_access(self):
        for seconds in [True,-1,0,float('nan'),float('inf'),'60',None]:
            self.assertFalse(c.can_read({'free':False,'remainRentSec':seconds}))
        self.assertTrue(c.can_read({'free':False,'remainRentSec':60}))

    def test_network_body_request_is_get_only(self):
        client=c.Client(cookie='session=synthetic')
        client.access_grants[('1','20')]={'purchased':True}
        seen=[]
        class Opener:
            def open(self,req,timeout):
                seen.append(req)
                return io.BytesIO(b'{"code":"M000_00000","result":{"entry":{"id":20,"content":"synthetic"}}}')
        client.opener=Opener();client.entry('1','20')
        self.assertEqual(seen[0].get_method(),'GET')
        self.assertIsNone(seen[0].data)


if __name__ == '__main__': unittest.main()
