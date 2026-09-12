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

  // Human phrasing for tool confirmations. The daemon sends the tool id and
  // its raw arguments; showing JSON to a person who is being asked a yes/no
  // question is asking them to parse a program instead of deciding.
  function describeConfirm(title) {
    const t = String(title || "")
    let m = t.match(/^(\w+)\s*(\{.*\})?\s*$/)
    if (!m) return t
    const name = m[1]
    let args = {}
    try { if (m[2]) args = JSON.parse(m[2]) } catch (e) { return t }
    switch (name) {
    case "open_app": return "Открыть " + (args.target || "приложение") + "?"
    case "browser_search": return "Открыть поиск: " + (args.query || "") + "?"
    case "focus_window": return "Переключиться на окно?"
    case "close_window": return "Закрыть это окно?"
    case "move_window": return "Перенести окно на стол " + (args.workspace || "?") + "?"
    case "workspace": return "Переключить на стол " + (args.number || "?") + "?"
    case "browser_control": return "Отправить команду браузеру (" + (args.action || "") + ")?"
    default: return "Выполнить " + name + "?"
    }
  }

  readonly property string statusText: {
    if (flashTimer.running) return "Прервано"
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

  // The clock that makes a long turn readable as work. Reuses the bar's rule:
  // count only while a question is out, and a moving number buys a lot of
  // patience compared to a still panel.
  property real now: 0
  Timer {
    interval: 1000
    repeat: true
    running: client.pendingSince > 0
    triggeredOnStart: true
    onTriggered: root.now = Date.now()
  }
  readonly property int waitedSeconds: client.pendingSince > 0
    ? Math.max(0, Math.round((root.now - client.pendingSince) / 1000))
    : 0

  // I pressed: acknowledge it for a moment. A key that does nothing visible
  // reads as broken, even when the daemon already did the right thing.
  Timer { id: flashTimer; interval: 1200; repeat: false }

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
    // The trace stream feeds the faint working behind the figure AND the
    // activity line under the status: the same line, read two ways.
    onTraced: function (text) {
      under.push(text)
      if (text.indexOf("инструмент:") === 0 || text.indexOf("ищу") === 0)
        client.activityText = text
    }
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
    function confirmPending(): bool { return client.confirmRequest !== null }
    function opened(): bool { return root.opened }
    function confirmTest(): void {
      client.confirmRequest = {
        id: 9999, prompt: "Тест диалога: видишь меня?",
        title: "open_app {\"target\": \"youtube.com\"}"
      }
    }
    function confirmAnswer(granted: bool): void {
      if (client.confirmRequest) client.replyConfirm(client.confirmRequest.id, granted === true)
    }
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
      // Anchored BOTTOM: the 3-finger-down gesture opens this panel, so it
      // rises from the bottom edge — the mirror of the workspace overview's
      // top strip (3-finger-up). Kept clear of the bar at the top.
      anchors.bottom: parent.bottom
      anchors.bottomMargin: Style.space(64)
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
          // A pending tool confirmation takes the keyboard over entirely:
          // Enter runs it, Esc declines it — nothing else may fire while a
          // destructive-ish action waits for a human decision.
          if (client.confirmRequest) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
              client.replyConfirm(client.confirmRequest.id, true)
              event.accepted = true
              return
            }
            if (event.key === Qt.Key_Escape) {
              client.replyConfirm(client.confirmRequest.id, false)
              event.accepted = true
              return
            }
            event.accepted = true
            return
          }
          // Auto-repeat must never reach push-to-talk: a held V that emits
          // synthetic press/release pairs commits the turn over and over, and
          // a whole phrase arrives as one-word fragments ("по", "то"). The
          // first press is the hold; repeats are noise.
          if (event.isAutoRepeat) {
            if (isKey(event, Qt.Key_V, 47, "м")) event.accepted = true
            return
          }
          if (event.key === Qt.Key_Escape) {
            root.dismiss()
            event.accepted = true
          } else if (isKey(event, Qt.Key_V, 47, "м")) {
            // Push-to-talk: V held = mic open, V released = turn committed.
            // The daemon gates the microphone on this signal; the turn ends
            // on release, so endpointing by silence never has to guess.
            if (!client.pttHeld) {
              client.pttHeld = true
              client.setPtt(true)
            }
            event.accepted = true
          } else if (isKey(event, Qt.Key_I, 23, "ш")) {
            client.cancel()
            flashTimer.restart()
            event.accepted = true
          } else if (isKey(event, Qt.Key_Q, 16, "й")) {
            root.endSession()
            event.accepted = true
          } else if (isKey(event, Qt.Key_N, 49, "т")) {
            client.reset()
            under.forget()
            event.accepted = true
          }
        }
        Keys.onReleased: function (event) {
          if (event.isAutoRepeat) {
            if (isKey(event, Qt.Key_V, 47, "м")) event.accepted = true
            return
          }
          if (isKey(event, Qt.Key_V, 47, "м") && client.pttHeld) {
            client.pttHeld = false
            client.setPtt(false)
            event.accepted = true
          }
        }
        // Losing focus (another window stolen, panel closing) must release a
        // held V: a release that never arrives leaves the mic open and the
        // turn uncommitted — the daemon listens to the room forever.
        onActiveFocusChanged: {
          if (!activeFocus && client.pttHeld) {
            client.pttHeld = false
            client.setPtt(false)
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
            height: statusRow.implicitHeight + (activityRow.visible ? activityRow.implicitHeight + Style.spacing.sm : 0)

            Column {
              id: statusRow
              anchors.left: parent.left
              anchors.right: parent.right
              spacing: Style.spacing.sm

              Row {
                spacing: Style.spacing.sm

                Rectangle {
                  id: statusDot
                  anchors.verticalCenter: parent.verticalCenter
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
                  anchors.verticalCenter: parent.verticalCenter
                  width: statusRow.width - statusDot.width - Style.spacing.sm - waitedChip.visible * (waitedChip.width + Style.spacing.sm)
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

                // The elapsed chip: 3s, 12s… It converts "the panel froze"
                // into "it has been N seconds", which is the difference
                // between distrust and patience.
                Text {
                  id: waitedChip
                  anchors.verticalCenter: parent.verticalCenter
                  visible: client.waiting && root.waitedSeconds > 0
                  text: root.waitedSeconds + " с"
                  color: Color.accent
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  opacity: 0.9
                }
              }

              // The live activity line: what the brain is doing right now
              // ("ищу в интернете…"), fed by the daemon's trace stream.
              Text {
                id: activityRow
                width: parent.width
                visible: client.waiting && client.activityText !== ""
                text: client.activityText
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
                color: Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                opacity: 0.85
              }
            }
          }
        }

        // --- footer: the keys ---------------------------------------------
        Item {
          id: footer
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          height: hint.implicitHeight + (capsLine.visible ? capsLine.implicitHeight + Style.spacing.xs : 0)

          Text {
            id: hint
            width: parent.width
            anchors.top: parent.top
            anchors.left: parent.left
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

          // What is actually under the hood, one small line: the brain on the
          // left, the ears on the right. A swapped backend or STT engine is
          // otherwise invisible until the answers change character.
          Text {
            id: capsLine
            width: parent.width
            anchors.top: hint.bottom
            anchors.topMargin: Style.spacing.xs
            anchors.left: parent.left
            visible: client.connected
            text: {
              const parts = []
              if (client.backend) parts.push(client.backend)
              if (client.sttEngine) parts.push(client.sttEngine === "handy" ? "GigaAM" : "vosk")
              return parts.join(" · ")
            }
            textFormat: Text.PlainText
            elide: Text.ElideRight
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            opacity: 0.25
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

            // Earlier turns of this session, oldest first, dimmer than the
            // live exchange. The panel used to forget every turn but the
            // last; a conversation you cannot scroll back through is not a
            // conversation.
            Repeater {
              model: Math.max(0, client.turns.length - 1)

              delegate: Column {
                width: middle.width
                spacing: Style.spacing.sm

                Text {
                  width: parent.width
                  text: client.turns[index] ? client.turns[index].user : ""
                  textFormat: Text.PlainText
                  wrapMode: Text.Wrap
                  color: Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  opacity: 0.35
                  visible: text !== ""
                }
                Text {
                  width: parent.width
                  text: client.turns[index] ? client.turns[index].answer : ""
                  textFormat: Text.PlainText
                  wrapMode: Text.Wrap
                  elide: Text.ElideRight
                  maximumLineCount: 3
                  color: Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  opacity: 0.45
                  visible: text !== ""
                }
              }
            }

            // The newest archived turn gets a separator before the live one.
            PanelSeparator {
              width: parent.width
              visible: client.turns.length > 0
            }

            // What the ears heard, above the answer: the transcription is the
            // half of the conversation that can silently go wrong (a missed
            // word is executed as a real query), so it gets its own line at
            // the top rather than being mixed into the waterfall below.
            Text {
              width: parent.width
              text: client.userText
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              opacity: 0.55
              visible: text !== ""
            }

            PanelSeparator {
              width: parent.width
              visible: client.userText !== "" && (answerPlain.visible || answerRich.visible)
            }

            // The answer, once. `assistantText` is what was spoken aloud;
            // `markdown` is the same answer with links and files attached.
            // Showing both was a duplicate — pick the richer one.
            Text {
              id: answerPlain
              width: parent.width
              text: client.assistantText
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              font.family: Style.font.resolvedFamily
              font.pixelSize: Style.font.body
              visible: text !== "" && client.markdown === ""
            }

            Text {
              id: answerRich
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

        // --- confirm dialog: a tool wants permission -------------------
        Rectangle {
          id: confirmCard
          visible: client.confirmRequest !== null
          anchors.centerIn: parent
          width: Math.min(parent.width - Style.space(24), Style.space(420))
          height: confirmCol.implicitHeight + Style.space(28)
          radius: Style.cornerRadius
          color: Color.menu.background
          border.color: Color.urgent
          border.width: Style.space(2)
          opacity: visible ? 1 : 0

          Behavior on opacity { NumberAnimation { duration: 120 } }

          Column {
            id: confirmCol
            anchors.top: parent.top
            anchors.topMargin: Style.space(14)
            anchors.left: parent.left
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.rightMargin: Style.space(14)
            spacing: Style.spacing.sm

            Text {
              width: parent.width
              text: client.confirmRequest
                ? root.describeConfirm(client.confirmRequest.title)
                : ""
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }

            Text {
              width: parent.width
              visible: text !== ""
              text: {
                // The raw tool call, for the rare case the human phrasing is
                // not enough. Dim and small, below the question, never above.
                const t = String(client.confirmRequest ? client.confirmRequest.title : "")
                const m = t.match(/^(\w+)\s*(\{.*\})?\s*$/)
                return m && m[2] ? m[1] + " " + m[2] : ""
              }
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              elide: Text.ElideRight
              maximumLineCount: 2
              color: Color.menu.text
              font.family: Style.font.resolvedFamily
              font.pixelSize: Style.font.caption
              opacity: 0.4
            }

            Row {
              spacing: Style.spacing.sm

              Rectangle {
                width: yesLbl.implicitWidth + Style.space(24)
                height: yesLbl.implicitHeight + Style.space(12)
                radius: Style.cornerRadius
                color: Color.accent
                MouseArea {
                  anchors.fill: parent
                  onClicked: client.replyConfirm(client.confirmRequest.id, true)
                }
                Text {
                  id: yesLbl
                  anchors.centerIn: parent
                  text: "Да (Enter)"
                  color: Color.background
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }
              }

              Rectangle {
                width: noLbl.implicitWidth + Style.space(24)
                height: noLbl.implicitHeight + Style.space(12)
                radius: Style.cornerRadius
                color: Color.menu.border
                MouseArea {
                  anchors.fill: parent
                  onClicked: client.replyConfirm(client.confirmRequest.id, false)
                }
                Text {
                  id: noLbl
                  anchors.centerIn: parent
                  text: "Нет (Esc)"
                  color: Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }
              }
            }
          }
        }

      }
    }
  }
}
