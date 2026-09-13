import unittest

from slack_messages import MAX_MARKDOWN_LENGTH, format_math_for_slack, split_markdown


class SlackMarkdownTests(unittest.TestCase):
    def test_latex_math_is_converted_to_readable_slack_format(self):
        cases = [
            ("Distance ($m$)", "Distance (m)"),
            (r"Speed ($\text{RPM}$)", "Speed (RPM)"),
            (r"RPM: $1255\text{ RPM}$", "RPM: 1255 RPM"),
            (r"Range: $1.34\text{ m} \le d \le 5.60\text{ m}$", "Range: 1.34 m ≤ d ≤ 5.60 m"),
            (r"Values: $x \ge 0$ and $y \approx 10$", "Values: x ≥ 0 and y ≈ 10"),
            (r"Angle: $45^\circ$", "Angle: 45°"),
            (r"Acceleration: $9.8\text{ m/s}^2$", "Acceleration: 9.8 m/s²"),
            (r"Fraction: $\frac{a + b}{c}$", "Fraction: (a + b) / c"),
            (r"Display: $$y = mx + b$$", "Display: y = mx + b"),
            (r"Bracket: \[v = \sqrt{x}\]", "Bracket: v = √(x)"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(format_math_for_slack(raw), expected)

    def test_math_cleaning_preserves_code_blocks_and_spans(self):
        text = (
            "Formula $x = 1$ in text.\n\n"
            "```cpp\n"
            "// do not touch $foo or \\text in code\n"
            "double $val = 10;\n"
            "```\n\n"
            "Inline `$code$` preserved."
        )
        cleaned = format_math_for_slack(text)
        self.assertIn("Formula x = 1 in text.", cleaned)
        self.assertIn("double $val = 10;", cleaned)
        self.assertIn("`$code$`", cleaned)

    def test_markdown_and_cpp_are_preserved_for_native_slack_rendering(self):
        answer = (
            "### Current limits\n\n"
            "**Verified** in [`FlywheelConstants.h`](https://github.com/team/robot/blob/main/FlywheelConstants.h).\n\n"
            "| Motor | Supply |\n| --- | --- |\n| Leader | 25 A |\n\n"
            "- Supply limit enabled\n- Stator limit enabled\n\n"
            "```cpp\nstd::vector<int> ids{1, 2};\nif (a < b && ready) { use(ids); }\n```"
        )
        self.assertEqual(split_markdown(answer), [answer])

    def test_long_prose_is_split_without_losing_text(self):
        for text in (("Motor configuration.\n\n" * 1_500).strip(), "a" * 25_000):
            with self.subTest(paragraphs="\n" in text):
                chunks = split_markdown(text)
                self.assertGreater(len(chunks), 1)
                self.assertEqual("".join(chunks), text)
                self.assertTrue(all(0 < len(chunk) <= MAX_MARKDOWN_LENGTH for chunk in chunks))

    def test_long_cpp_block_is_closed_and_reopened_with_language(self):
        code = "if (current < limit && enabled) { apply(config); }\n" * 700
        chunks = split_markdown("```cpp\n" + code + "```")
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith("```cpp\n") and chunk.endswith("```") for chunk in chunks))
        self.assertTrue(all(len(chunk) <= MAX_MARKDOWN_LENGTH for chunk in chunks))
        self.assertEqual("".join(chunk[len("```cpp\n"):-3] for chunk in chunks), code)

    def test_exact_limit_is_not_split(self):
        text = "a" * MAX_MARKDOWN_LENGTH
        self.assertEqual(split_markdown(text), [text])

    def test_malformed_long_fence_header_still_makes_progress(self):
        text = "```" + "x" * 30_000
        chunks = split_markdown(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= MAX_MARKDOWN_LENGTH for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
