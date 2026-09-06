"""Routing regressions: a speaker route must carry a working AEC pair."""

import unittest
from unittest.mock import patch

from omavoice import devices


class DeviceRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sources = {"laptop-mic", "usb-mic"}
        self.sinks = {"laptop-speaker", "wired-headphones"}
        self.default_source = "laptop-mic"
        self.default_sink = "laptop-speaker"
        self.ports = {
            "laptop-speaker": "analog-output-speaker",
            "wired-headphones": "analog-output-headphones",
        }
        self.sink_properties = {}
        self.pactl = patch.object(devices, "_pactl", side_effect=self.fake_pactl)
        self.pactl.start()
        self.addCleanup(self.pactl.stop)

    async def fake_pactl(self, *args):
        if args == ("get-default-sink",):
            return self.default_sink
        if args == ("get-default-source",):
            return self.default_source
        if args == ("list", "short", "sources"):
            return "\n".join(f"{i}\t{name}\tmodule" for i, name in enumerate(self.sources))
        if args == ("list", "short", "sinks"):
            return "\n".join(f"{i}\t{name}\tmodule" for i, name in enumerate(self.sinks))
        if args == ("list", "sinks"):
            return "\n".join(
                "\n".join(
                    [
                        f"Sink #{i}",
                        f"Name: {name}",
                        "Properties:",
                        *(
                            f'    {key} = "{value}"'
                            for key, value in self.sink_properties.get(name, {}).items()
                        ),
                        f"Active Port: {self.ports.get(name, '')}",
                    ]
                )
                for i, name in enumerate(self.sinks)
            )
        if args == ("list", "sources"):
            return "\n".join(f"Name: {name}\nDescription: {name}" for name in self.sources)
        self.fail(f"unexpected pactl invocation: {args}")

    def add_aec(self):
        self.sources.add(devices.AEC_SOURCE)
        self.sinks.add(devices.AEC_SINK)

    def assert_protected(self, route):
        self.assertFalse(route.headphones)
        self.assertFalse(route.echo_cancelled)
        self.assertIn("half duplex", route.summary)
        self.assertIn("voice interruption unavailable", route.summary)

    async def test_missing_aec_protects_physical_speaker_route(self):
        route = await devices.resolve("", "")
        self.assertEqual((route.input_target, route.output_target),
                         ("laptop-mic", "laptop-speaker"))
        self.assert_protected(route)

    async def test_source_without_sink_does_not_select_nonexistent_aec_output(self):
        self.sources.add(devices.AEC_SOURCE)
        route = await devices.resolve("", "")
        self.assertEqual(route.output_target, "laptop-speaker")
        self.assertEqual(route.input_target, "laptop-mic")
        self.assert_protected(route)

    async def test_sink_without_source_stays_protected(self):
        self.sinks.add(devices.AEC_SINK)
        route = await devices.resolve("", "")
        self.assertEqual(route.input_target, "laptop-mic")
        self.assertEqual(route.output_target, "laptop-speaker")
        self.assert_protected(route)

    async def test_complete_aec_pair_enables_full_duplex(self):
        self.add_aec()
        route = await devices.resolve("", "")
        self.assertEqual((route.input_target, route.output_target),
                         (devices.AEC_SOURCE, devices.AEC_SINK))
        self.assertTrue(route.echo_cancelled)
        self.assertFalse(route.headphones)
        self.assertEqual(route.fallback_input, "")

    async def test_explicit_complete_pair_is_recognized(self):
        self.add_aec()
        route = await devices.resolve(devices.AEC_SOURCE, devices.AEC_SINK)
        self.assertTrue(route.echo_cancelled)

    async def test_explicit_mismatched_pairs_keep_intent_but_require_protection(self):
        self.add_aec()
        for source, sink in ((devices.AEC_SOURCE, "laptop-speaker"),
                             ("laptop-mic", devices.AEC_SINK)):
            with self.subTest(source=source, sink=sink):
                route = await devices.resolve(source, sink)
                self.assertEqual((route.input_target, route.output_target), (source, sink))
                self.assert_protected(route)

    async def test_explicit_plain_output_is_not_claimed_as_aec(self):
        self.add_aec()
        route = await devices.resolve("", "laptop-speaker")
        self.assertEqual(route.output_target, "laptop-speaker")
        self.assert_protected(route)

    async def test_selected_default_mic_can_use_aec_when_output_is_automatic(self):
        self.add_aec()
        route = await devices.resolve("laptop-mic", "")
        self.assertEqual(route.input_target, devices.AEC_SOURCE)
        self.assertTrue(route.echo_cancelled)

    async def test_selected_other_mic_is_protected(self):
        self.add_aec()
        route = await devices.resolve("usb-mic", "")
        self.assertEqual(route.input_target, "usb-mic")
        self.assertEqual(route.output_target, "laptop-speaker")
        self.assert_protected(route)

    async def test_explicit_headphones_override_default_speakers(self):
        route = await devices.resolve("laptop-mic", "wired-headphones")
        self.assertEqual(route.output_target, "wired-headphones")
        self.assertTrue(route.headphones)
        self.assertFalse(route.echo_cancelled)

    async def test_explicit_speakers_override_default_headphones(self):
        self.default_sink = "wired-headphones"
        route = await devices.resolve("", "laptop-speaker")
        self.assertEqual(route.output_target, "laptop-speaker")
        self.assert_protected(route)

    async def test_missing_explicit_headphones_do_not_disable_speaker_protection(self):
        route = await devices.resolve("laptop-mic", "bluez_output.missing")
        self.assertEqual(route.output_target, "laptop-speaker")
        self.assert_protected(route)

    async def test_missing_selected_microphone_returns_to_complete_aec_pair(self):
        self.add_aec()
        route = await devices.resolve("unplugged-mic", "")
        self.assertEqual(route.input_target, devices.AEC_SOURCE)
        self.assertEqual(route.output_target, devices.AEC_SINK)
        self.assertTrue(route.echo_cancelled)

    async def test_bluetooth_headphones_keep_their_own_microphone(self):
        self.default_sink = "bluez_output.AA_BB_CC_DD_EE_FF.1"
        headset_mic = "bluez_input.AA:BB:CC:DD:EE:FF"
        self.sinks.add(self.default_sink)
        self.sources.add(headset_mic)
        self.sink_properties[self.default_sink] = {
            "device.form_factor": "headset",
            "device.icon_name": "audio-headset-bluetooth",
        }
        route = await devices.resolve("", "")
        self.assertEqual(route.input_target, headset_mic)
        self.assertEqual(route.fallback_input, "laptop-mic")
        self.assertTrue(route.headphones)
        self.assertFalse(route.echo_cancelled)

    async def test_failed_bluetooth_microphone_uses_default_without_changing_output(self):
        self.default_sink = "bluez_output.AA_BB_CC_DD_EE_FF.1"
        headset_mic = "bluez_input.AA:BB:CC:DD:EE:FF"
        self.sinks.add(self.default_sink)
        self.sources.add(headset_mic)
        self.sink_properties[self.default_sink] = {
            "device.form_factor": "headphone",
        }
        route = await devices.resolve("", "", avoid={headset_mic})
        self.assertEqual(route.input_target, "laptop-mic")
        self.assertEqual(route.output_target, self.default_sink)
        self.assertTrue(route.headphones)

    async def test_bluetooth_speaker_with_microphone_is_not_treated_as_headphones(self):
        self.default_sink = "bluez_output.AA_BB_CC_DD_EE_FF.1"
        self.sinks.add(self.default_sink)
        self.sources.add("bluez_input.AA:BB:CC:DD:EE:FF")
        self.sink_properties[self.default_sink] = {
            "device.form_factor": "speaker",
            "device.icon_name": "audio-speakers-bluetooth",
            "device.product.name": "Portable Speakerphone",
        }

        route = await devices.resolve("", "")

        self.assertEqual(route.output_target, self.default_sink)
        self.assert_protected(route)

    async def test_bluetooth_speaker_uses_complete_aec_pair_when_available(self):
        self.default_sink = "bluez_output.AA_BB_CC_DD_EE_FF.1"
        self.sinks.add(self.default_sink)
        self.sink_properties[self.default_sink] = {
            "device.form_factor": "speaker",
            "device.icon_name": "audio-speakers-bluetooth",
        }
        self.add_aec()

        route = await devices.resolve("", "")

        self.assertEqual(
            (route.input_target, route.output_target),
            (devices.AEC_SOURCE, devices.AEC_SINK),
        )
        self.assertTrue(route.echo_cancelled)
        self.assertFalse(route.headphones)

    async def test_bluetooth_soundbar_is_not_treated_as_headphones(self):
        self.default_sink = "bluez_output.11_22_33_44_55_66.1"
        self.sinks.add(self.default_sink)
        self.sink_properties[self.default_sink] = {
            "device.form-factor": "tv",
            "device.icon-name": "audio-speakers-bluetooth",
            "node.description": "Samsung Soundbar",
        }

        route = await devices.resolve("", "")

        self.assert_protected(route)

    async def test_unknown_bluetooth_output_fails_closed_as_a_speaker(self):
        self.default_sink = "bluez_output.11_22_33_44_55_66.1"
        self.sinks.add(self.default_sink)

        route = await devices.resolve("", "")

        self.assert_protected(route)
        self.assertIn("bluetooth output without headphone evidence", route.reason)

    async def test_bluetooth_headphones_can_be_recognized_by_icon(self):
        self.default_sink = "bluez_output.AA_BB_CC_DD_EE_FF.1"
        self.sinks.add(self.default_sink)
        self.sink_properties[self.default_sink] = {
            "device.icon_name": "audio-headphones-bluetooth",
        }

        route = await devices.resolve("", "")

        self.assertTrue(route.headphones)

    async def test_explicit_speaker_form_factor_beats_a_misleading_icon(self):
        self.default_sink = "bluez_output.11_22_33_44_55_66.1"
        self.sinks.add(self.default_sink)
        self.sink_properties[self.default_sink] = {
            "device.form_factor": "speaker",
            "device.icon_name": "audio-headset-bluetooth",
        }

        route = await devices.resolve("", "")

        self.assert_protected(route)

    async def test_hands_free_speakerphone_is_not_assumed_to_be_worn(self):
        self.default_sink = "bluez_output.11_22_33_44_55_66.1"
        self.sinks.add(self.default_sink)
        self.sink_properties[self.default_sink] = {
            "device.form_factor": "hands-free",
        }

        route = await devices.resolve("", "")

        self.assert_protected(route)

    async def test_handsfree_port_name_is_not_headphone_evidence(self):
        self.default_sink = "bluez_output.11_22_33_44_55_66.1"
        self.sinks.add(self.default_sink)
        self.ports[self.default_sink] = "headset-output-handsfree"

        route = await devices.resolve("", "")

        self.assert_protected(route)

    async def test_usb_headset_is_recognized_without_a_name_hint(self):
        self.default_sink = "alsa_output.usb-Generic_Audio.analog-stereo"
        self.sinks.add(self.default_sink)
        self.sink_properties[self.default_sink] = {
            "device.form-factor": "headset",
        }

        route = await devices.resolve("", "")

        self.assertTrue(route.headphones)
        self.assertFalse(route.echo_cancelled)


if __name__ == "__main__":
    unittest.main()
