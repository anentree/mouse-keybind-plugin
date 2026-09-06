import QtQuick
import qs.Commons

// Horizontal slider for the mouse panel. Derived from Omarchy's Ui/PanelSlider
// with two deliberate differences:
//   * the mouse wheel never changes the value — it scrolls the panel like
//     everywhere else (stock PanelSlider eats wheel events and nudges the knob);
//   * dragging is not stolen by the surrounding ScrollView when the pointer
//     drifts vertically (preventStealing), and values snap to `step` while
//     dragging so the label and the knob agree.
Item {
  id: root

  property QtObject bar: null
  property real value: 0
  property real minimum: 0
  property real maximum: 1
  property real step: 0.05
  property color trackColor: bar ? Style.selectedFillFor(bar.foreground, Color.accent) : "#333"
  property color fillColor: bar ? bar.foreground : Color.foreground
  property color knobColor: bar ? bar.foreground : Color.foreground
  property bool dragging: false
  property real trackHeight: Math.max(4, Math.round(Style.spacing.controlHeight * 0.11))
  property real knobSize: Math.max(14, Math.round(Style.spacing.controlHeight * 0.38))
  // What the knob shows: the committed value, or the pointer position while dragging.
  property real liveValue: value

  onValueChanged: if (!dragging) liveValue = value

  signal moved(real value)
  signal released(real value)

  implicitWidth: Style.space(200)
  implicitHeight: Math.max(Style.space(22), knobSize + Style.spacing.md)

  readonly property real range: Math.max(0.0001, maximum - minimum)
  readonly property real progress: Math.max(0, Math.min(1, (liveValue - minimum) / range))
  readonly property bool _hot: mouseArea.containsMouse || root.dragging

  function snap(raw) {
    var stepped = root.step > 0 ? Math.round((raw - root.minimum) / root.step) * root.step + root.minimum : raw
    // trim float noise (0.30000000000000004 -> 0.3)
    stepped = Math.round(stepped * 1000) / 1000
    return Math.max(root.minimum, Math.min(root.maximum, stepped))
  }

  Rectangle {
    id: track
    anchors.verticalCenter: parent.verticalCenter
    anchors.left: parent.left
    anchors.right: parent.right
    height: root.trackHeight
    radius: height / 2
    color: root.trackColor
  }

  Rectangle {
    id: fill
    anchors.verticalCenter: track.verticalCenter
    anchors.left: track.left
    height: track.height
    radius: track.radius
    color: root.fillColor
    width: track.width * root.progress

    Behavior on width {
      enabled: !root.dragging
      NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
    }
  }

  BorderSurface {
    id: knob
    width: root.knobSize
    height: root.knobSize
    radius: root.knobSize / 2
    color: root.knobColor
    borderSpec: Border.flat(root.bar ? root.bar.background : "#101315", Math.max(1, Style.space(2)))
    anchors.verticalCenter: track.verticalCenter
    x: Math.max(0, Math.min(track.width - width, track.width * root.progress - width / 2))
    scale: root._hot ? 1.15 : 1.0

    Behavior on x {
      enabled: !root.dragging
      NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
    }

    Behavior on scale {
      NumberAnimation { duration: 110; easing.type: Easing.OutCubic }
    }
  }

  MouseArea {
    id: mouseArea
    anchors.fill: parent
    hoverEnabled: true
    preventStealing: true          // the ScrollView above us must not take the drag
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton
    // No onWheel handler on purpose: wheel events fall through to the panel's scroller.

    function valueFromX(x) {
      var clamped = Math.max(0, Math.min(track.width, x))
      return root.snap(root.minimum + (clamped / track.width) * root.range)
    }

    onPressed: function(mouse) {
      root.dragging = true
      var next = valueFromX(mouse.x)
      root.liveValue = next
      root.moved(next)
    }
    onPositionChanged: function(mouse) {
      if (!root.dragging) return
      var next = valueFromX(mouse.x)
      if (next === root.liveValue) return
      root.liveValue = next
      root.moved(next)
    }
    onReleased: function(mouse) {
      if (!root.dragging) return
      root.dragging = false
      var final = valueFromX(mouse.x)
      root.liveValue = final
      root.released(final)
    }
    onCanceled: {
      root.dragging = false
      root.liveValue = root.value
    }
  }
}
