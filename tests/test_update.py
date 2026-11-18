import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import update  # noqa: E402


def rfc(dt, two_digit_year=False):
    s = format_datetime(dt)                      # Wed, 18 Nov 2026 08:00:00 +0000
    if two_digit_year:
        parts = s.split(" ")
        parts[3] = parts[3][2:]
        s = " ".join(parts)
    return s


def make_fixtures(folder):
    now = datetime.now(timezone.utc)
    t1, t2, t3 = now - timedelta(hours=1), now - timedelta(hours=2), now - timedelta(hours=3)
    story = "Asian Development Bank approves $500m loan for Pakistan pension reforms"

    (folder / "dawn.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Dawn</title>
<item>
  <title>{story}</title>
  <link>https://www.dawn.com/news/1</link>
  <description>&lt;p&gt;The bank &amp;amp; Pakistan signed the loan deal.&lt;/p&gt;</description>
  <pubDate>{rfc(t1)}</pubDate>
  <media:content url="https://img/1.jpg"><media:title>Asian Development Bank Headquarters in Manila</media:title>
  <media:description>Photo caption only</media:description></media:content>
</item></channel></rss>""", encoding="utf-8")

    (folder / "the-express-tribune.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Tribune</title>
<item><title>ADB approves $500m loan for Pakistan pension reforms</title>
<link>https://tribune.com.pk/story/2</link>
<description>Two-digit year date test.</description>
<pubDate>{rfc(t2, two_digit_year=True)}</pubDate></item></channel></rss>""", encoding="utf-8")

    (folder / "dw.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="x"><title>DW</title></channel>
<item rdf:about="y"><title>Germany debates new defence budget &amp; conscription</title>
<link>https://www.dw.com/en/a-3</link><description>DW excerpt.</description>
<dc:date>{t3.strftime('%Y-%m-%dT%H:%M:%SZ')}</dc:date></item></rdf:RDF>""", encoding="utf-8")

    (folder / "the-guardian.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Guardian</title>
<item><title>Blow to Kremlin as bridge struck by missile</title><link>https://www.theguardian.com/world/4</link>
<description>Good morning. Ukraine has damaged a bridge. Plus what is in store for a new 007?</description>
<pubDate>{rfc(t1)}</pubDate></item>
<item><title>Floods displace thousands in southern Sindh province</title><link>https://www.theguardian.com/world/5</link>
<description>Rain &nbsp; flooding continues.</description><pubDate>{rfc(t2)}</pubDate></item>
</channel></rss>""", encoding="utf-8")


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        make_fixtures(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self, name, source):
        return update.parse_feed((self.dir / name).read_bytes(), source)

    def test_dawn_uses_real_headline_not_image_caption(self):
        items = self.parse("dawn.xml", "Dawn")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["title"].startswith("Asian Development Bank approves"))
        self.assertIn("signed the loan deal", items[0]["snippet"])
        self.assertNotIn("Photo caption", items[0]["snippet"])

    def test_two_digit_year(self):
        items = self.parse("the-express-tribune.xml", "Tribune")
        self.assertEqual(items[0]["published"].year, datetime.now(timezone.utc).year)

    def test_rdf_feed_and_dc_date(self):
        items = self.parse("dw.xml", "DW")
        self.assertEqual(len(items), 1)
        self.assertIn("&", items[0]["title"])
        self.assertEqual(items[0]["published"].year, datetime.now(timezone.utc).year)

    def test_newsletter_digest_skipped_and_entity_fixed(self):
        items = self.parse("the-guardian.xml", "Guardian")
        self.assertEqual([i["title"] for i in items], ["Floods displace thousands in southern Sindh province"])

    def test_grouping_across_sources(self):
        items = (self.parse("dawn.xml", "Dawn") + self.parse("the-express-tribune.xml", "Tribune")
                 + self.parse("dw.xml", "DW"))
        items.sort(key=lambda i: i["published"], reverse=True)
        events = update.group(items)
        sizes = sorted(len(e) for e in events)
        self.assertEqual(sizes, [1, 2])

    def test_parse_ai_handles_code_fences(self):
        text = '```json\n{"stories":[{"id":"a","short_summary":"x","key_facts":["f", 3]}]}\n```'
        out = update.parse_ai(text)
        self.assertEqual(out["a"]["key_facts"], ["f"])
        self.assertEqual(out["a"]["detailed_summary"], "")


class EndToEnd(unittest.TestCase):
    def test_mock_run_writes_valid_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            make_fixtures(d)
            out = d / "out.json"
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "update.py"), "--mock",
                                "--fixtures", str(d), "--out", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["ai"]["total"], 3)
            self.assertEqual(data["ai"]["summarised"], 3)
            multi = [e for e in data["events"] if len(e["sources"]) == 2]
            self.assertEqual(len(multi), 1)
            self.assertNotIn("snippet", json.dumps(data))       # no article text published
            # Second run reuses summaries instead of asking the AI again
            r2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "update.py"), "--mock",
                                 "--fixtures", str(d), "--out", str(out)],
                                capture_output=True, text=True)
            data2 = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data2["ai"]["requests"], 0)
            self.assertEqual(data2["ai"]["summarised"], 3)


if __name__ == "__main__":
    unittest.main()
