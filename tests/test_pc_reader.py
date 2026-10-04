"""PC protocol tests; no account credentials or novel text in fixtures."""
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_gold import pc_reader as p
from munpia_gold import core as c


def frame(kind, value):
    b=json.dumps(value).encode()
    return bytes([kind])+len(b).to_bytes(4,'big')+b


def stream(indices=(0,1)):
    return frame(1,{'publicKey':'fake','totalChunks':2})+b''.join(frame(2,{'index':i,'content':'fake'}) for i in indices)+frame(3,{'totalChunks':2})


class PCTests(unittest.TestCase):
    def test_complete_and_reordered_frames(self):
        self.assertEqual(p.parse_frames(stream((1,0))), ('fake',['fake','fake']))
        self.assertEqual(p.parse_frames(stream((1,2))), ('fake',['fake','fake']))

    def test_missing_duplicate_or_truncated_frames(self):
        for data in (stream((0,0)),stream((0,3)),stream()[:-1],stream()+b'\x00',stream()[5:]):
            with self.assertRaises((ValueError,KeyError,TypeError)):
                p.parse_frames(data)

    def test_unowned_pc_request_never_reaches_network(self):
        client=c.Client()
        with patch.object(client.opener,'open') as opened:
            with self.assertRaises(c.AccessDenied):
                p.request(client,'1','2','/api/v1/pc/novel-detail/1/entries/2/content',{'signature':'fake'})
            opened.assert_not_called()

    def test_only_body_post_is_allowed(self):
        client=c.Client()
        with patch.object(client.opener,'open') as opened:
            for path,payload in [('/api/purchase',{}),('/api/v1/pc/novel-detail/1/entries/2/info',{}),('/api/v1/pc/novel-detail/1/entries/2/content',None)]:
                with self.assertRaises(c.MunpiaError):
                    p.request(client,'1','2',path,payload)
            opened.assert_not_called()

    def test_mobile_app_notice_falls_back_only_after_grant(self):
        client=c.Client(transport=lambda path,params:{'code':'A002_21014'})
        with patch.object(p,'entry',return_value=({'id':2},'synthetic')) as fallback:
            with self.assertRaises(c.AccessDenied):client.entry('1','2')
            fallback.assert_not_called()
            client.access_grants[('1','2')]={'purchased':True}
            self.assertEqual(client.entry('1','2')[1],'synthetic')
            fallback.assert_called_once_with(client,'1','2')

    def test_missing_node_is_actionable(self):
        with patch.object(p.shutil,'which',return_value=None):
            with self.assertRaisesRegex(c.MunpiaError,'Node.js 18'):
                p.Runtime(c.Client())

    def test_pc_does_not_forward_mobile_host_cookie(self):
        from io import BytesIO
        class Response(BytesIO):
            headers={'Content-Type':'application/json'}
        cookies=[{'name':'parent','value':'synthetic','domain':'.munpia.com','path':'/'},
                 {'name':'mobile','value':'synthetic','domain':'m.munpia.com','hostOnly':True,'path':'/'}]
        client=c.Client(cookie=json.dumps(cookies))
        client.access_grants[('1','2')]={'purchased':True}
        with patch.object(client.opener,'open',return_value=Response(b'{"code":"M000_00000","result":{}}')) as opened:
            p.request(client,'1','2','/api/v1/pc/novel-detail/1/entries/2/info')
        req=opened.call_args.args[0]
        self.assertEqual(req.get_header('Cookie'),'parent=synthetic')
        self.assertEqual(req.get_method(),'GET')

if __name__=='__main__':unittest.main()
