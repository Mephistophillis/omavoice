pragma ComponentBehavior: Bound

// The floating voice panel — stripped to the essentials: the waveform, the
// live transcript under it, a status line and the answer.
//
// No onboarding, no consent tour, no settings window: the daemon runs the
// local engine (vosk + edge-tts) with the hermes gateway as the brain, all
// configured in ~/.config/omavoice/env, and none of it needs a wizard.
//
// The panel owns the microphone's lifetime. Opening it starts a session,
// closing it ends one — a voice assistant that listens while you are not
// looking at it is not a thing worth building.

import QtQuick
import QtQuick.Effects
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  // Injected by the shell host on load.
  property var shell: null
  property var manifest: null

  // The host reads this back; without it `toggle` desyncs after the first open.
  property bool opened: false
  property string keyError: ""

  StateHues { id: hues }
  readonly property color hintGlow:
    hues.colorFor(client.voiceState, Color.menu.background, Color.accent)

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.baranskyi.omavoice"

  readonly property string statusText: {
    if (client.errorText) return client.errorText
    if (!client.connected) return "Daemon not running"
    switch (client.voiceState) {
    case "listening": return client.pttHeld ? "Listening…" : "Hold V and speak"
    case "thinking": return "Looking it up"
    case "speaking": return "Answering"
    case "error": return "Error"
    default: return "Hold V to talk"
    }
  }

  function open(payloadJson) {
    root.opened = true
    client.wanted = true
    if (!client.connected || !client.backgrounded) client.clearConversation()
    client.startSession()
    client.foreground()
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  // Host-initiated close. It already knows, so do not tell it back.
  // The session survives: an agent that is mid-thought should finish, and the
  // answer should still be spoken. The microphone stops, so nothing is
  // listening to a room with no window on screen.
  function close() {
    root.opened = false
    client.background()
    client.wanted = false
  }

  // User-initiated close (Escape, click on the scrim). Tell the host so its
  // openPanelIds map stays in step and the next toggle opens rather than closes.
  function dismiss() {
    root.opened = false
    client.background()
    client.wanted = false
    if (shell && typeof shell.hide === "function") shell.hide(root.pluginId)
  }

  // The deliberate ending: hang up, drop the session, release everything.
  function endSession() {
    root.opened = false
    client.stopSession()
    client.wanted = false
    if (shell && typeof shell.hide === "function") shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  Client {
    id: client
    // Felt, not read: the figure buzzes the moment the assistant is talked
    // over, before the state has caught up.
    onBarged: wave.bargeIn()
    onTraced: function (text) { under.push(text) }
    onConnectedChanged: {
      if (connected && root.opened) {
        startSession()
        foreground()
      }
    }
  }

  // The bar widget reads state through the host, so expose it by name.
  readonly property string voiceState: client.voiceState
  readonly property bool daemonConnected: client.connected

  IpcHandler {
    target: "io.github.baranskyi.omavoice"
    function open(): void { root.open("{}") }
    function close(): void { root.dismiss() }
    function toggle(): void { root.toggle() }
    function reset(): void { client.reset() }
    function state(): string { return client.voiceState }
    function backend(): string { return client.backend }
  }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"

    WlrLayershell.namespace: "omavoice"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim.a > 0.05
        ? Color.menu.scrim
        : Qt.rgba(Color.background.r, Color.background.g, Color.background.b, 0.55)
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.top: parent.top
      anchors.topMargin: Style.space(64)
      width: Style.space(560)
      height: Math.min(
        Style.space(620),
        card.contentTopInset + card.contentBottomInset
          + head.implicitHeight
          + (middle.implicitHeight > 0 ? Style.spacing.panelGap + middle.implicitHeight : 0)
          + Style.spacing.panelGap + footer.height
      )
      radius: Style.cornerRadius
      color: Color.menu.background
      borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.panelPadding

      // Swallow clicks so hitting the card does not dismiss the panel.
      MouseArea { anchors.fill: parent; onClicked: {} }

      Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }

      Item {
        id: keyCatcher
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        clip: true
        focus: true
        Keys.priority: Keys.BeforeItem
        // Hotkeys must survive layout switching. Three ways to identify the
        // same physical key, because no single one is reliable here:
        //   1. Qt.Key_* — works in the Latin layout only; with Russian
        //      selected the V key delivers a Cyrillic keysym, no Qt.Key_V.
        //   2. nativeScanCode — layout-independent in theory, but its value
        //      depends on the platform (X hands out evdev+8, Wayland may
        //      hand out the raw evdev code, and 0 has been seen), so both
        //      variants are accepted.
        //   3. The Cyrillic letter the key types in the Russian layout
        //      (QWERTY-ЙЦУКЕН: V=м, Q=й, I=ш, N=т) — text matching, which
        //      covers scan-code-less environments entirely.
        // The panel takes exclusive keyboard focus, so any keypress arriving
        // here is aimed at us; false positives are not a concern.
        function isKey(event, qtKey, evdev, cyr) {
          if (event.key === qtKey) return true
          const sc = event.nativeScanCode || 0
          if (sc === evdev || sc === evdev + 8) return true
          if (cyr && event.text && event.text.toLowerCase() === cyr) return true
          return false
        }
        Keys.onPressed: function (event) {
          if (event.key === Qt.Key_Escape) {
            root.dismiss()
            event.accepted = true
          } else if (root.isKey(event, Qt.Key_V, 47, "м")) {
            // Push-to-talk: V held = mic open, V released = turn committed.
            // The daemon gates the microphone on this signal; the turn ends
            // on release, so endpointing by silence never has to guess.
            if (!client.pttHeld) {
              client.pttHeld = true
              client.setPtt(true)
            }
            event.accepted = true
          } else if (root.isKey(event, Qt.Key_I, 23, "ш")) {
            client.cancel()
            event.accepted = true
          } else if (root.isKey(event, Qt.Key_Q, 16, "й")) {
            root.endSession()
            event.accepted = true
          } else if (root.isKey(event, Qt.Key_N, 49, "т")) {
            client.reset()
            under.forget()
            event.accepted = true
          }
        }
        Keys.onReleased: function (event) {
          if (root.isKey(event, Qt.Key_V, 47, "м") && client.pttHeld) {
            client.pttHeld = false
            client.setPtt(false)
            event.accepted = true
          }
        }

        // Fixed head: the waveform with the transcript under it, then the
        // status line. They stay put while the answer scrolls underneath.
        Column {
          id: head
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.panelGap

          // --- waveform + live transcript -------------------------------
          Item {
            width: parent.width
            height: wave.implicitHeight + Style.space(16)

            Undertext {
              id: under
              anchors.fill: parent
              anchors.leftMargin: Style.spaceReal(10)
              anchors.rightMargin: Style.spaceReal(10)
              anchors.bottomMargin: Style.spaceReal(6)
              working: client.voiceState === "thinking" || client.waiting
              tint: root.hintGlow
              light: wave.light
            }

            Waveform {
              id: wave
              anchors.centerIn: parent
              width: parent.width
              height: Style.spaceReal(120)
              voiceState: client.connected ? client.voiceState : "error"
              level: client.level
              bands: client.bands
              accent: Color.accent
              dim: Color.menu.text
            }
          }

          // --- status line ----------------------------------------------
          Item {
            width: parent.width
            height: statusText.implicitHeight

            Rectangle {
              id: statusDot
              anchors.left: parent.left
              anchors.top: parent.top
              anchors.topMargin: Math.round((statusText.implicitHeight - height) / 2)
              width: Style.space(8)
              height: Style.space(8)
              radius: width / 2
              color: client.voiceState === "listening" ? Color.accent
                   : client.voiceState === "error" || !client.connected ? Color.urgent
                   : Color.menu.text
              opacity: client.voiceState === "idle" ? 0.4 : 1

              SequentialAnimation on opacity {
                running: client.voiceState === "listening"
                loops: Animation.Infinite
                NumberAnimation { to: 0.35; duration: 700; easing.type: Easing.InOutQuad }
                NumberAnimation { to: 1.0; duration: 700; easing.type: Easing.InOutQuad }
              }
            }

            Text {
              id: statusText
              anchors.left: statusDot.right
              anchors.leftMargin: Style.spacing.sm
              anchors.right: parent.right
              anchors.top: parent.top
              text: root.statusText
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              maximumLineCount: 3
              elide: Text.ElideRight
              color: client.voiceState === "error" || !client.connected ? Color.urgent : Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              opacity: 0.9
            }
          }
        }

        // --- footer: the keys ---------------------------------------------
        Item {
          id: footer
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          height: hint.implicitHeight

          Text {
            id: hint
            width: parent.width
            text: client.connected
              ? "Esc — background · I — interrupt · N — new · Q — stop"
              : "Start the daemon:  systemctl --user start omavoice"
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            opacity: 0.35
          }
        }

        // The answer, scrolling under the head.
        Flickable {
          id: scroller
          anchors.top: head.bottom
          anchors.topMargin: Style.spacing.panelGap
          anchors.bottom: footer.top
          anchors.bottomMargin: Style.spacing.panelGap
          anchors.left: parent.left
          anchors.right: parent.right
          contentHeight: middle.implicitHeight
          clip: true
          interactive: contentHeight > height
          boundsBehavior: Flickable.StopAtBounds

          Column {
            id: middle
            width: scroller.width
            spacing: Style.spacing.panelGap

            Text {
              width: parent.width
              text: client.assistantText
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              font.family: Style.font.resolvedFamily
              font.pixelSize: Style.font.body
              visible: text !== ""
            }

            PanelSeparator {
              width: parent.width
              visible: client.markdown !== "" && client.assistantText !== ""
            }

            Text {
              width: parent.width
              text: {
                const t = String(client.markdown || "")
                return t
                  .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
                  .replace(/!\[[^\]]*\]\s*\[[^\]]*\]/g, "")
                  .replace(/^\s*\[[^\]]+\]:\s*\S+.*$/gm, "")
                  .replace(/<\s*img\b[^>]*>/gi, "")
              }
              textFormat: Text.MarkdownText
              wrapMode: Text.Wrap
              color: Color.menu.text
              font.family: Style.font.resolvedFamily
              font.pixelSize: Style.font.body
              visible: text !== ""
            }
          }
        }

        Rectangle {
          id: fadeTop
          readonly property color bg: Color.menu.background
          anchors.left: scroller.left
          anchors.right: scroller.right
          anchors.top: scroller.top
          height: Style.space(20)
          visible: scroller.contentHeight > scroller.height && !scroller.atYBeginning
          gradient: Gradient {
            GradientStop { position: 0.0; color: fadeTop.bg }
            GradientStop { position: 1.0; color: Qt.rgba(fadeTop.bg.r, fadeTop.bg.g, fadeTop.bg.b, 0) }
          }
        }

        Rectangle {
          id: fadeBottom
          readonly property color bg: Color.menu.background
          anchors.left: scroller.left
          anchors.right: scroller.right
          anchors.bottom: scroller.bottom
          height: Style.space(20)
          visible: scroller.contentHeight > scroller.height && !scroller.atYEnd
          gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.rgba(fadeBottom.bg.r, fadeBottom.bg.g, fadeBottom.bg.b, 0) }
            GradientStop { position: 1.0; color: fadeBottom.bg }
          }
        }

      }
    }
  }
}
