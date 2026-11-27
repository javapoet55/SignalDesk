"""Each ranked table in the scan report offers its tickers as comma-separated text."""

import re
import unittest

import server as sv

ROW = "<tr><td class='n'>{i}</td><td><a href='https://finviz.com/quote.ashx?t={q}'>{t}</a></td><td>Co</td></tr>"


def section(slug, name, tickers, diagnostics=True):
    rows = "".join(ROW.format(i=i, q=t.replace("&amp;", "%26"), t=t) for i, t in enumerate(tickers, 1))
    table = f"<div class='wrap'><table><tbody>{rows}</tbody></table></div>" if tickers else "<p class='muted'>None.</p>"
    tail = "<details><summary>Diagnostics</summary><div>Source</div></details>" if diagnostics else ""
    return f"<section class='card' id='{slug}'><header><h3>{name}</h3></header>{table}{tail}</section>"


def report(*sections):
    return ("<!doctype html><html><head><title>r</title></head><body><main><h2>Top 25 by screen</h2>"
            + "".join(sections) + "</main></body></html>").encode()


class CopySymbols(unittest.TestCase):
    def boxes(self, page):
        return re.findall(r"<details class='report-symbols'><summary>Copy Symbols</summary><div>"
                          r"<textarea readonly rows='2' aria-label='([^']*)'>([^<]*)</textarea>"
                          r"<button type='button' data-copy-symbols>Copy</button></div></details>", page)

    def test_every_table_gets_its_own_symbols_in_table_order(self):
        page = sv.themed_report(report(
            section("momentum-change", "2 Day Momentum", ["HPQ", "SNDQ", "COGT"]),
            section("gainers-change", "Top Gainers", ["BRK-B", "HPQ", "BF.B"]),
        )).decode()
        self.assertEqual(self.boxes(page), [
            ("Symbols in 2 Day Momentum, comma separated", "HPQ,SNDQ,COGT"),
            ("Symbols in Top Gainers, comma separated", "BRK-B,HPQ,BF.B"),
        ])
        # The box sits inside its own section, just above Diagnostics, and Note links stay out of the list.
        first = page.split("<section class='card' id='momentum-change'>")[1].split("</section>")[0]
        self.assertLess(first.index("</table>"), first.index("Copy Symbols"))
        self.assertLess(first.index("Copy Symbols"), first.index("<summary>Diagnostics</summary>"))
        self.assertIn("class='report-note'", first)
        self.assertNotIn("Note", self.boxes(page)[0][1])
        self.assertEqual(page.count('<script id="report-copy-symbols">'), 1)

    def test_empty_tables_duplicates_and_missing_diagnostics(self):
        page = sv.themed_report(report(
            section("empty-change", "Empty", []),
            section("dupes-change", "Dupes", ["AAA", "BBB", "AAA"], diagnostics=False),
        )).decode()
        self.assertEqual(self.boxes(page), [("Symbols in Dupes, comma separated", "AAA,BBB")])
        dupes = page.split("id='dupes-change'>")[1]
        self.assertTrue(dupes.split("</section>")[0].endswith("</details>"))

    def test_other_cards_and_non_reports_are_untouched(self):
        summary = "<div class='card wrap'><table><tbody>" + ROW.format(i=1, q="ZZZ", t="ZZZ") + "</tbody></table></div>"
        page = sv.themed_report(report(summary)).decode()
        self.assertEqual(self.boxes(page), [])
        self.assertEqual(sv.themed_report(b"plain text"), b"plain text")

    def test_real_report_markup(self):
        import finviz_agent as fa

        source = open(fa.__file__).read()
        # The served page relies on these exact pieces of the generated report.
        self.assertIn("<section class='card' id='", source)
        self.assertIn("https://finviz.com/quote.ashx?t=", source)
        self.assertIn("<details><summary>Diagnostics</summary>", source)


if __name__ == "__main__":
    unittest.main()
