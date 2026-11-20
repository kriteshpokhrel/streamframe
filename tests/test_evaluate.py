import copy
import json
import subprocess
import sys
import unittest

from streamframe.evaluate import InputError, evaluate


FIXTURE = {
    "samples": [
        {
            "id": "clip-a",
            "transcript": {
                "reference": [
                    {"word": "one", "start": 0, "end": 1},
                    {"word": "two"},
                    {"word": "three", "start": 2, "end": 3},
                    {"word": "four"},
                ],
                "prediction": [
                    {"word": "one", "start": 0.2, "end": 1.1},
                    {"word": "too"},
                    {"word": "three", "start": 2.4, "end": 3.2},
                    {"word": "extra"},
                    {"word": "four"},
                ],
            },
            "localization": {
                "reference": [{"start": 0, "end": 10}, {"start": 20, "end": 30}],
                "prediction": [
                    {"start": 0, "end": 10}, {"start": 0, "end": 10},
                    {"start": 20, "end": 26}, {"start": 40, "end": 50},
                ],
            },
            "face_crops": [
                {"reference": [0, 0, 10, 10], "prediction": [0, 0, 5, 10]},
                {"reference": [0, 0, 10, 10], "prediction": [-5, -5, 15, 15]},
                {"reference": [0, 0, 10, 10], "prediction": None},
            ],
        },
        {
            "id": "clip-b",
            "transcript": {
                "reference": [{"word": "lost"}],
                "prediction": [],
            },
            "localization": {"reference": [], "prediction": [{"start": 0, "end": 1}]},
        },
    ]
}


class EvaluateTests(unittest.TestCase):
    def test_metrics_and_pooled_aggregation(self):
        result = evaluate(FIXTURE, top_k=2)
        a = result["samples"][0]
        transcript = a["transcript"]
        self.assertEqual((transcript["substitutions"], transcript["deletions"], transcript["insertions"]), (1, 0, 1))
        self.assertEqual(transcript["matched_words"], 3)
        self.assertEqual([item["operation"] for item in transcript["alignment"]],
                         ["match", "substitution", "match", "insertion", "match"])
        self.assertEqual(transcript["alignment"][3],
                         {"operation": "insertion", "reference_index": None, "prediction_index": 3})
        self.assertEqual(transcript["wer"], 0.5)
        self.assertEqual(transcript["timed_matches"], 2)
        self.assertAlmostEqual(transcript["mean_start_absolute_error"], 0.3)
        self.assertAlmostEqual(transcript["mean_end_absolute_error"], 0.15)
        self.assertEqual(result["aggregate"]["transcript"]["wer"], 3 / 5)
        low = a["localization"]["0.5"]
        self.assertEqual((low["matched_events"], low["duplicate_predictions"]), (2, 1))
        self.assertEqual(low["recall"], 1)
        self.assertEqual(low["precision_at_k"], 0.5)
        self.assertEqual(low["mean_start_absolute_error"], 0)
        self.assertEqual(low["mean_end_absolute_error"], 2)
        self.assertEqual(a["localization"]["0.75"]["recall"], 0.5)
        self.assertEqual(result["aggregate"]["localization"]["0.5"]["precision_at_k"], 1 / 3)
        faces = a["face_crops"]
        self.assertEqual(faces["predicted_crops"], 2)
        self.assertEqual(faces["mean_iou"], 0.25)
        self.assertEqual(faces["mean_containment"], 0.5)
        self.assertIsNone(result["samples"][1]["localization"]["0.5"]["recall"])

    def test_matching_requires_augmenting_path(self):
        data = {"samples": [{"id": "a", "localization": {
            "reference": [{"start": 0, "end": 10}, {"start": 5, "end": 15}],
            "prediction": [{"start": 2, "end": 12}, {"start": 0, "end": 6}],
        }}]}
        metrics = evaluate(data, thresholds=[0.5], top_k=2)["aggregate"]["localization"]["0.5"]
        self.assertEqual(metrics["matched_events"], 2)
        self.assertEqual(metrics["duplicate_predictions"], 0)
        self.assertEqual(metrics["precision_at_k"], 1)

    def test_empty_denominators_and_case_sensitive_words(self):
        data = {"samples": [{"id": "empty", "transcript": {
            "reference": [], "prediction": [{"word": "Hello"}],
        }, "localization": {"reference": [], "prediction": []}, "face_crops": []},
            {"id": "case", "transcript": {
                "reference": [{"word": "hello"}], "prediction": [{"word": "Hello"}],
            }}]}
        result = evaluate(data)
        empty = result["samples"][0]
        self.assertIsNone(empty["transcript"]["wer"])
        self.assertEqual(empty["transcript"]["insertions"], 1)
        self.assertIsNone(empty["localization"]["0.5"]["precision_at_k"])
        self.assertIsNone(empty["face_crops"]["mean_iou"])
        self.assertEqual(result["samples"][1]["transcript"]["substitutions"], 1)
        self.assertEqual(result["aggregate"]["transcript"]["wer"], 2)

    def test_large_numbers_do_not_overflow_iou(self):
        data = {"samples": [{"id": "large", "localization": {
            "reference": [{"start": 0.0, "end": 1e308}],
            "prediction": [{"start": 0.0, "end": 1e308}],
        }, "face_crops": [{
            "reference": [0.0, 0.0, 1e154, 1e154],
            "prediction": [0.0, 0.0, 1e154, 1e154],
        }]}]}
        result = evaluate(data)
        self.assertEqual(result["aggregate"]["localization"]["0.5"]["recall"], 1)
        self.assertEqual(result["aggregate"]["face_crops"]["mean_iou"], 1)
        data["samples"][0]["face_crops"][0]["reference"] = [0, 0, 1e-300, 1e-300]
        with self.assertRaisesRegex(InputError, "representable"):
            evaluate(data)

    def test_validation(self):
        cases = []
        bad = copy.deepcopy(FIXTURE)
        bad["samples"][0]["transcript"]["reference"][0].pop("end")
        cases.append((bad, "start and end"))
        bad = copy.deepcopy(FIXTURE)
        bad["samples"][0]["localization"]["reference"][0]["end"] = 0
        cases.append((bad, "start < end"))
        bad = copy.deepcopy(FIXTURE)
        bad["samples"][0]["face_crops"][0]["prediction"] = [0, 0, 0, 1]
        cases.append((bad, "x1 < x2"))
        bad = copy.deepcopy(FIXTURE)
        bad["samples"][0]["transcript"]["reference"][0]["word"] = "two words"
        cases.append((bad, "single word"))
        bad = copy.deepcopy(FIXTURE)
        bad["samples"][1]["id"] = "clip-a"
        cases.append((bad, "duplicate sample"))
        bad = copy.deepcopy(FIXTURE)
        bad["samples"][0]["face_crops"][0]["reference"][0] = True
        cases.append((bad, "finite number"))
        bad = copy.deepcopy(FIXTURE)
        bad["samples"][0]["transcript"]["predictions"] = []
        cases.append((bad, "unknown fields"))
        cases.extend([({"samples": []}, "at least one"), ({"samples": [{"id": "a"}]}, "component")])
        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(InputError, message):
                evaluate(data)
        for thresholds in ([], [0], [1.01], [0.5, 0.5], [float("nan")], [True]):
            with self.subTest(thresholds=thresholds), self.assertRaises(InputError):
                evaluate(FIXTURE, thresholds=thresholds)
        for top_k in (0, -1, True, 1.5):
            with self.subTest(top_k=top_k), self.assertRaises(InputError):
                evaluate(FIXTURE, top_k=top_k)

    def cli(self, *args, input_text=""):
        return subprocess.run([sys.executable, "-B", "-m", "streamframe.evaluate", *args],
                              input=input_text, text=True, capture_output=True, check=False)

    def test_cli_json_stdin_help_and_resource_option(self):
        help_result = self.cli("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("face_crops", help_result.stdout)
        self.assertIn("--iou-thresholds", help_result.stdout)
        text = json.dumps(FIXTURE)
        run = self.cli("-", "--top-k", "2", input_text=text)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), evaluate(FIXTURE, top_k=2))
        self.assertEqual(run.stdout, self.cli("-", "--top-k", "2", input_text=text).stdout)
        measured = self.cli("-", "--measure-resources", input_text=text)
        self.assertEqual(measured.returncode, 0, measured.stderr)
        self.assertGreaterEqual(json.loads(measured.stdout)["resources"]["wall_seconds"], 0)

    def test_cli_reports_errors(self):
        for text in ("{", '{"samples": [], "samples": []}', '{"samples": NaN}'):
            run = self.cli("-", input_text=text)
            self.assertEqual(run.returncode, 2)
            self.assertIn("error:", run.stderr)
            self.assertNotIn("Traceback", run.stderr)
        for args in (("--iou-thresholds", "bad"), ("--top-k", "0")):
            run = self.cli("-", *args, input_text=json.dumps(FIXTURE))
            self.assertEqual(run.returncode, 2)
            self.assertIn("error:", run.stderr)
        missing = self.cli("this-file-does-not-exist.json")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("error:", missing.stderr)


if __name__ == "__main__":
    unittest.main()
