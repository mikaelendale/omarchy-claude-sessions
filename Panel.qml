import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Claude Code sessions: one bar icon that says whether anything is working,
// watching or waiting on you, and a panel listing every session. Picking a
// session focuses its terminal window.
Panel {
  id: root
  moduleName: "mike.claude-sessions"
  ipcTarget: "mike.claude-sessions"
  manageIpc: false

  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")).replace(/\/$/, "")
  readonly property string notifyMode: {
    var value = String(setting("notify", "Unfocused")).toLowerCase()
    return value === "always" || value === "off" ? value : "unfocused"
  }

  // Items inside the panel's lazily built content, null until it first opens.
  readonly property Item panelFlick: panel.loadedContent ? panel.loadedContent.panelFlick : null
  readonly property Item rowColumn: panel.loadedContent ? panel.loadedContent.rowColumn : null

  property var sessions: []
  property var counts: ({ needs: 0, done: 0, working: 0, watching: 0, idle: 0 })
  property bool received: false
  property int spinFrame: 0
  property double nowMs: Date.now()
  property bool cursorActive: false
  property int cursorIndex: 0

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property int needsCount: counts.needs || 0
  readonly property int doneCount: counts.done || 0
  readonly property bool anyWorking: (counts.working || 0) > 0
  readonly property bool anySessions: sessions.length > 0
  readonly property bool spinning: anyWorking && !Style.reduceMotion
  readonly property string workGlyph: spinning ? (spinFrame === 0 ? "◐" : "◑") : "✳"

  readonly property string barGlyph: needsCount === 0 && anyWorking ? workGlyph : "✳"
  readonly property color barIconColor: needsCount > 0
    ? urgent
    : (anySessions ? barForeground : Qt.darker(barForeground, 1.55))
  readonly property string barLabel: needsCount > 0
    ? Commons.I18n.formatNumber(needsCount)
    : (doneCount > 0 ? Commons.I18n.formatNumber(doneCount) : "")

  readonly property var stateOrder: ["needs", "done", "working", "watching", "idle"]
  readonly property string summary: {
    var parts = []
    for (var i = 0; i < stateOrder.length; i++) {
      var n = counts[stateOrder[i]] || 0
      if (n > 0) parts.push(strings.tn("summary." + stateOrder[i], n).arg(Commons.I18n.formatNumber(n)))
    }
    return parts.length > 0 ? parts.join(" · ") : strings.t("summary.none")
  }

  // Sections in the order they matter, each with its rows and their place in
  // the keyboard order.
  readonly property var groups: {
    var result = []
    var index = 0
    for (var s = 0; s < stateOrder.length; s++) {
      var rows = []
      for (var i = 0; i < sessions.length; i++) {
        if (sessions[i].state === stateOrder[s]) rows.push({ session: sessions[i], index: index++ })
      }
      if (rows.length > 0) result.push({ state: stateOrder[s], rows: rows })
    }
    return result
  }
  readonly property var orderedSessions: {
    var list = []
    for (var g = 0; g < groups.length; g++)
      for (var r = 0; r < groups[g].rows.length; r++) list.push(groups[g].rows[r].session)
    return list
  }
  // Every session row, then the "New session" row.
  readonly property int cursorCount: orderedSessions.length + 1

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  // ---------------------------------------------------------------- data

  function limited(value, limit) {
    var s = value === undefined || value === null ? "" : String(value)
    return s.length > limit ? s.substring(0, limit) : s
  }

  function applyLine(line) {
    var data
    try { data = JSON.parse(line) } catch (e) { return }
    if (!data || !Array.isArray(data.sessions)) return
    var list = []
    var tally = { needs: 0, done: 0, working: 0, watching: 0, idle: 0 }
    for (var i = 0; i < data.sessions.length && i < 64; i++) {
      var s = data.sessions[i]
      if (!s || typeof s !== "object") continue
      var state = tally.hasOwnProperty(s.state) ? s.state : "idle"
      var window = /^0x[0-9a-f]{1,16}$/.test(String(s.window || "")) ? String(s.window) : ""
      tally[state]++
      list.push({
        id: limited(s.id, 120),
        label: limited(s.label, 120),
        project: limited(s.project, 60),
        state: state,
        reason: limited(s.reason, 20),
        detail: limited(s.detail, 120),
        since: Number(s.since) > 0 ? Number(s.since) : 0,
        window: window,
        tmux: s.tmux === true
      })
    }
    sessions = list
    counts = tally
    received = true
    if (cursorIndex >= cursorCount) cursorIndex = Math.max(0, cursorCount - 1)
  }

  function ago(since) {
    if (!since) return ""
    var minutes = Math.floor(Math.max(0, nowMs - since) / 60000)
    if (minutes < 1) return strings.t("ago.now")
    if (minutes < 60) return strings.t("ago.minutes").arg(Commons.I18n.formatNumber(minutes))
    var hours = Math.floor(minutes / 60)
    if (hours < 24) return strings.t("ago.hours").arg(Commons.I18n.formatNumber(hours))
    return strings.t("ago.days").arg(Commons.I18n.formatNumber(Math.floor(hours / 24)))
  }

  function stateLine(session) {
    if (session.state === "needs") {
      if (session.reason !== "other" && session.reason !== "") return strings.t("reason." + session.reason, strings.t("state.needs"))
      return session.detail !== "" ? strings.t("reason.other").arg(session.detail) : strings.t("state.needs")
    }
    if (session.state === "idle" && session.detail !== "") return strings.t("detail.idleRaw").arg(session.detail)
    return strings.t("detail." + session.state)
  }

  function metaLine(session) {
    var parts = [stateLine(session)]
    if (session.project !== "") parts.push(session.project)
    if (session.window === "") parts.push(session.tmux ? strings.t("inTmux") : strings.t("noWindow"))
    var when = ago(session.since)
    if (when !== "") parts.push(when)
    return parts.join(" · ")
  }

  function stateGlyph(state) {
    if (state === "working") return workGlyph
    if (state === "watching") return "◉"
    if (state === "idle") return "○"
    return "●"
  }

  function stateColor(state) {
    if (state === "needs") return urgent
    if (state === "idle") return dim
    return foreground
  }

  // ------------------------------------------------------------- actions

  function focusSession(session) {
    if (!session || session.window === "") return false
    // The address is checked against ^0x[0-9a-f]+$ when it arrives.
    Hyprland.dispatch('hl.dsp.focus({ window = "address:' + session.window + '" })')
    close()
    return true
  }

  // The session that has waited on you longest, then the oldest finished one.
  function jump() {
    for (var i = 0; i < sessions.length; i++)
      if (sessions[i].state === "needs" && focusSession(sessions[i])) return "ok"
    for (var j = 0; j < sessions.length; j++)
      if (sessions[j].state === "done" && focusSession(sessions[j])) return "ok"
    if (!opened) open()
    return "none"
  }

  function newSession() {
    Quickshell.execDetached(["omarchy-agent"])
    close()
  }

  function moveCursor(dy) {
    if (dy === 0) return
    cursorIndex = Math.max(0, Math.min(cursorCount - 1, cursorIndex + dy))
    scrollCursorIntoView()
  }

  function activateCursor() {
    if (cursorIndex < orderedSessions.length) focusSession(orderedSessions[cursorIndex])
    else newSession()
  }

  function setCursor(index) {
    cursorActive = true
    cursorIndex = index
  }

  function scrollCursorIntoView() {
    if (!panelFlick || !rowColumn) return
    Qt.callLater(function() {
      var item = rowColumn.rowAt(root.cursorIndex)
      if (!item || !panelFlick) return
      var margin = Style.space(6)
      var top = item.mapToItem(panelFlick.contentItem, 0, 0).y
      var bottom = top + item.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < panelFlick.contentY + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > panelFlick.contentY + panelFlick.height - margin)
        panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  onOpenedChanged: if (opened) {
    nowMs = Date.now()
    cursorActive = false
    cursorIndex = 0
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function() { if (panel.loadedContent) panel.loadedContent.forceActiveFocus() })
  }

  // -------------------------------------------------------------- helper

  onNotifyModeChanged: if (collector.running) collector.running = false

  Process {
    id: collector
    command: ["python3", "-I", root.pluginDir + "/sessions.py", "--notify", root.notifyMode]
    stdout: SplitParser { onRead: function(line) { root.applyLine(line) } }
    onExited: restartTimer.start()
  }

  Timer {
    id: restartTimer
    interval: 2000
    onTriggered: if (root.pluginDir !== "") collector.running = true
  }

  Component.onCompleted: if (pluginDir !== "") collector.running = true

  Timer {
    interval: 960
    repeat: true
    running: root.spinning
    onTriggered: root.spinFrame = 1 - root.spinFrame
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.opened
    onTriggered: root.nowMs = Date.now()
  }

  ShellIpc {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function jump(): string { return root.jump() }
    function status(): string { return root.summary }
  }

  // ----------------------------------------------------------------- bar

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barGlyph
    foreground: root.barIconColor
    labelText: root.barLabel
    tooltipText: root.opened ? "" : root.summary
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.jump()
      else if (buttonCode === Qt.MiddleButton) root.newSession()
      else root.toggle()
    }
  }

  // --------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(560))

    content: PanelKeyCatcher {
      id: keyCatcher
      property alias panelFlick: panelFlick
      property alias rowColumn: rowColumn
      implicitHeight: column.implicitHeight
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "n" || t === "N") root.newSession()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: strings.t("title")
            meta: root.received ? root.summary : strings.t("starting")
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.barGlyph
                color: root.needsCount > 0 ? root.urgent : (root.anySessions ? root.foreground : root.dim)
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          PanelSeparator {
            visible: root.anySessions
            foreground: root.foreground
          }

          Column {
            id: rowColumn
            visible: root.anySessions
            width: parent.width
            spacing: Style.space(12)

            function rowAt(index) {
              for (var g = 0; g < groupRepeater.count; g++) {
                var group = groupRepeater.itemAt(g)
                var row = group ? group.rowAt(index) : null
                if (row) return row
              }
              return index === root.orderedSessions.length ? newRow : null
            }

            Repeater {
              id: groupRepeater
              model: root.groups

              Column {
                id: groupColumn
                required property var modelData
                width: rowColumn.width
                spacing: Style.space(4)

                function rowAt(index) {
                  for (var r = 0; r < rowRepeater.count; r++) {
                    var row = rowRepeater.itemAt(r)
                    if (row && row.rowIndex === index) return row
                  }
                  return null
                }

                PanelSectionHeader {
                  text: strings.t("state." + groupColumn.modelData.state)
                  foreground: groupColumn.modelData.state === "needs" ? root.urgent : root.foreground
                  fontFamily: root.fontFamily
                }

                Repeater {
                  id: rowRepeater
                  model: groupColumn.modelData.rows

                  SessionRow {
                    required property var modelData
                    width: groupColumn.width
                    session: modelData.session
                    rowIndex: modelData.index
                  }
                }
              }
            }
          }

          Text {
            visible: root.received && !root.anySessions
            width: parent.width
            text: strings.t("empty")
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
          }

          PanelSeparator { foreground: root.foreground }

          NewSessionRow {
            id: newRow
            width: parent.width
          }
        }
      }
    }
  }

  component SessionRow: CursorSurface {
    id: sessionRow
    property var session: ({})
    property int rowIndex: 0
    readonly property bool reachable: session && session.window !== ""

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    foreground: root.foreground
    implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: sessionRow.reachable ? Qt.PointingHandCursor : Qt.ArrowCursor
      onEntered: root.setCursor(sessionRow.rowIndex)
      onClicked: root.focusSession(sessionRow.session)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      Text {
        textFormat: Text.PlainText
        text: root.stateGlyph(sessionRow.session.state)
        color: root.stateColor(sessionRow.session.state)
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        Layout.alignment: Qt.AlignVCenter
        Layout.preferredWidth: Style.space(14)
        horizontalAlignment: Text.AlignHCenter
      }

      ColumnLayout {
        id: rowContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: sessionRow.session.label
          color: sessionRow.session.state === "idle" ? root.dim : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: root.metaLine(sessionRow.session)
          color: sessionRow.session.state === "needs" ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component NewSessionRow: CursorSurface {
    id: newSessionRow
    readonly property int rowIndex: root.orderedSessions.length

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    foreground: root.foreground
    implicitHeight: newContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setCursor(newSessionRow.rowIndex)
      onClicked: root.newSession()
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      Text {
        textFormat: Text.PlainText
        text: "+"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        Layout.alignment: Qt.AlignVCenter
        Layout.preferredWidth: Style.space(14)
        horizontalAlignment: Text.AlignHCenter
      }

      ColumnLayout {
        id: newContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: strings.t("newSession")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: strings.t("newSessionHint")
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }
}
