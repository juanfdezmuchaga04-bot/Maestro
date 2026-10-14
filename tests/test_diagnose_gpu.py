"""The S145 diagnosis must identify hardware from measurements and never claim a backend works."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "diagnose_gpu", Path(__file__).resolve().parents[1] / "scripts" / "diagnose_gpu.py")
diagnose_gpu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diagnose_gpu)


def windows_runner(answers):
    def run(command, timeout):
        script = " ".join(command)
        for marker, payload in answers.items():
            if marker in script:
                return 0, json.dumps(payload)
        return 127, ""
    return run


class DiagnoseGpuTests(unittest.TestCase):
    def test_pci_id_parsing(self):
        self.assertEqual(diagnose_gpu.pci_id_from_pnp(r"PCI\VEN_1002&DEV_15D8&SUBSYS_380917AA&REV_C2\4&1"),
                         "1002:15d8")
        self.assertEqual(diagnose_gpu.pci_id_from_pnp("ROOT\\BasicDisplay"), "")
        gpus = diagnose_gpu.parse_lspci(
            "00:00.0 Host bridge [0600]: AMD [1022:15d0]\n"
            "03:00.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. [AMD/ATI] Picasso/Raven 2 "
            "[Radeon Vega Series / Radeon Vega Mobile Series] [1002:15d8] (rev c2)\n")
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["pci_id"], "1002:15d8")

    def test_s145_api_on_windows_is_identified_but_untested(self):
        runner = windows_runner({
            "Win32_ComputerSystem": {"Manufacturer": "LENOVO", "Model": "81UT", "SystemFamily": "IdeaPad S145-15API",
                                     "TotalPhysicalMemory": 8 * 1024 ** 3},
            "Win32_OperatingSystem": {"Caption": "Microsoft Windows 11 Home", "Version": "10.0.22631",
                                      "BuildNumber": "22631"},
            "Win32_Processor": {"Name": "AMD Ryzen 5 3500U with Radeon Vega Mobile Gfx"},
            "Win32_VideoController": {"Name": "AMD Radeon(TM) Vega 8 Graphics",
                                      "PNPDeviceID": r"PCI\VEN_1002&DEV_15D8&SUBSYS_3F1A17AA",
                                      "DriverVersion": "31.0.12027.9001", "AdapterRAM": 2147483648},
        })
        report = diagnose_gpu.collect(runner=runner, system="Windows")
        self.assertEqual(report["machine"]["family"], "IdeaPad S145-15API")
        self.assertEqual(report["ram_mb"], 8192)
        gpu = report["gpus"][0]
        self.assertIn("Picasso", gpu["known"]["name"])
        self.assertEqual(report["backends"]["rocm"]["status"], "not_applicable")
        self.assertIn(report["backends"]["directml"]["status"], {"candidate_untested", "installed_untested"})
        self.assertNotIn("probe_passed", json.dumps(report["backends"]))
        self.assertIn("S145-14API", diagnose_gpu.summary(report))

    def test_linux_amd_apu_rocm_is_reported_unsupported(self):
        def runner(command, timeout):
            if command[0] == "lspci":
                return 0, ("04:00.0 VGA compatible controller [0300]: AMD [AMD/ATI] Stoney [Radeon R2/R3/R4/R5 "
                           "Graphics] [1002:98e4] (rev eb)\n")
            return 127, ""
        report = diagnose_gpu.collect(runner=runner, system="Linux")
        self.assertEqual(report["backends"]["rocm"]["status"], "unsupported_by_vendor")
        self.assertEqual(report["backends"]["directml"]["status"], "not_applicable")

    def test_unknown_gpu_is_not_guessed(self):
        def runner(command, timeout):
            if command[0] == "lspci":
                return 0, "01:00.0 VGA compatible controller [0300]: Vendor X [abcd:1234]\n"
            return 127, ""
        report = diagnose_gpu.collect(runner=runner, system="Linux")
        self.assertIsNone(report["gpus"][0]["known"])
        self.assertIn("not in Maestro's table", diagnose_gpu.summary(report))


if __name__ == "__main__":
    unittest.main()
