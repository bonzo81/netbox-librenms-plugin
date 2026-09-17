class TestAssertions:
    def test_unittest_style(self, value, other):
        # ruleid: no-unittest-assertions
        self.assertEqual(1, 1)
        # ruleid: no-unittest-assertions
        self.assertNotEqual(1, 2)
        # ruleid: no-unittest-assertions
        self.assertTrue(True)
        # ruleid: no-unittest-assertions
        self.assertFalse(False)
        # ruleid: no-unittest-assertions
        self.assertIn(1, [1])
        # ruleid: no-unittest-assertions
        self.assertNotIn(2, [1])
        # ruleid: no-unittest-assertions
        self.assertIsNone(None)
        # ruleid: no-unittest-assertions
        self.assertIsNotNone(1)
        # ruleid: no-unittest-assertions
        self.assertIs(value, value)
        # ruleid: no-unittest-assertions
        self.assertIsNot(value, other)
        # ruleid: no-unittest-assertions
        self.assertIsInstance(value, int)
        # ruleid: no-unittest-assertions
        self.assertNotIsInstance(value, str)
        # ruleid: no-unittest-assertions
        self.assertAlmostEqual(1.0, 1.0)
        # ruleid: no-unittest-assertions
        self.assertNotAlmostEqual(1.0, 2.0)
        # ruleid: no-unittest-assertions
        self.assertGreater(2, 1)
        # ruleid: no-unittest-assertions
        self.assertGreaterEqual(2, 2)
        # ruleid: no-unittest-assertions
        self.assertLess(1, 2)
        # ruleid: no-unittest-assertions
        self.assertLessEqual(2, 2)
        # ruleid: no-unittest-assertions
        self.assertCountEqual([1], [1])
        # ruleid: no-unittest-assertions
        self.assertSequenceEqual([1], [1])
        # ruleid: no-unittest-assertions
        self.assertListEqual([1], [1])
        # ruleid: no-unittest-assertions
        self.assertTupleEqual((1,), (1,))
        # ruleid: no-unittest-assertions
        self.assertSetEqual({1}, {1})
        # ruleid: no-unittest-assertions
        self.assertDictEqual({}, {})
        # ruleid: no-unittest-assertions
        self.assertMultiLineEqual("x", "x")
        # ruleid: no-unittest-assertions
        self.assertRegex("text", "t")
        # ruleid: no-unittest-assertions
        self.assertNotRegex("text", "z")
        # ruleid: no-unittest-assertions
        self.assertRaises(ValueError, parse)
        # ruleid: no-unittest-assertions
        self.assertRaisesRegex(ValueError, "invalid", parse)
        # ruleid: no-unittest-assertions
        self.assertWarns(UserWarning, warn)
        # ruleid: no-unittest-assertions
        self.assertWarnsRegex(UserWarning, "warning", warn)
        # ruleid: no-unittest-assertions
        self.assertLogs("test", level="INFO")
        # ruleid: no-unittest-assertions
        self.assertNoLogs("test")
        # ruleid: no-unittest-assertions
        with self.assertRaises(ValueError):
            parse()

    def test_pytest_style(self, value):
        # ok: no-unittest-assertions
        assert value == 1
        # ok: no-unittest-assertions
        with pytest.raises(ValueError):
            parse()
        # ok: no-unittest-assertions
        self.assert_valid(value)
        # ok: no-unittest-assertions
        collaborator.assertEqual(1, 1)


class TestResponse:
    def assertResponseUnchanged(self, before, after):
        assert before == after

    def test_response(self):
        # ok: no-unittest-assertions
        self.assertResponseUnchanged({"status": "ready"}, {"status": "ready"})
