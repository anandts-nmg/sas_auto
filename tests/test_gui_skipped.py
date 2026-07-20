import pytest


@pytest.mark.gui
@pytest.mark.skip(reason="GUI tests run only during an explicitly requested interactive calibration session")
def test_interactive_gui_calibration():
    pass
