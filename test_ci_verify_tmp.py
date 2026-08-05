import unittest


class TestDeliberateFailure(unittest.TestCase):
    """Throwaway: exists only to verify the CI check actually fails and
    branch protection actually blocks merge. Removed before this branch
    is done with -- never meant to land on main."""

    def test_deliberately_fails(self):
        self.assertEqual(1, 2)


if __name__ == "__main__":
    unittest.main()
