import json
import tempfile
import unittest
from pathlib import Path
from windows_config import DEFAULTS, validate, positive, save, load

class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(validate({}), DEFAULTS)
    def test_nonfinite(self):
        for k in ("max_order","max_exposure","slippage","cooldown","last_seconds"):
            for v in ("nan","inf","-inf",True,"abc"):
                with self.subTest(k=k,v=v), self.assertRaises(ValueError):
                    validate({k:v})
    def test_constraints(self):
        for raw in ({"max_order":30,"max_exposure":10},{"slippage":.2},{"cooldown":100.5},
                    {"signature_type":9},{"signature_type":True},{"leaderboard":"yes"},
                    {"funder":"0x"+"0"*40},{"version":2},{"private_key":"not allowed"}):
            with self.subTest(raw=raw),self.assertRaises(ValueError): validate(raw)
    def test_positive(self):
        for v in (True,0,-1,"nan","inf"):
            with self.subTest(v=v),self.assertRaises(ValueError): positive(v)
        self.assertEqual(positive("5.25"),5.25)
    def test_atomic_store(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"config.json"
            self.assertEqual(load(p),DEFAULTS)
            save(p,{"max_order":2})
            original=p.read_bytes()
            with self.assertRaises(ValueError): save(p,{"private_key":"SECRET"})
            self.assertEqual(p.read_bytes(),original)
            self.assertEqual(load(p)["max_order"],2)
            self.assertEqual(list(Path(d).glob("*.tmp")),[])
    def test_invalid_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"config.json"
            p.write_text("not json")
            with self.assertRaises(ValueError): load(p)

if __name__ == '__main__': unittest.main()
