! ===========================================================================
!  Host watchdog for the 5-axis stage control panel.
!
!  The UI jogs by holding a key: press starts motion, release stops it. That
!  stop depends on the host still being alive to send it. If the UI freezes,
!  its process is killed, or the network drops, the release never arrives and
!  the axis keeps moving -- and the linear stages (0, 1) have no limit
!  switches, so "keeps moving" ends at the mechanical stop.
!
!  This program is the only safeguard that survives losing the host, because
!  it runs on the controller and triggers on the host's ABSENCE.
!
!  Protocol:
!    HOSTWDOG    host increments this at least every WATCHDOG_PERIOD_MS
!    HOSTWDEN    host sets 1 to arm, 0 to disarm (clean disconnect)
!    HOSTWDTMO   timeout in ms; host sets at arm time
!    HOSTWDFIRED set to 1 by this program when it trips; host clears
!
!  A polling loop is used rather than an ON autoroutine because autoroutines
!  are edge-triggered: "TIME > deadline" stays true once tripped and would
!  not re-arm cleanly across reconnects.
! ===========================================================================

GLOBAL INT HOSTWDOG          ! heartbeat counter written by the host
GLOBAL INT HOSTWDEN          ! 1 = armed, 0 = disarmed
GLOBAL INT HOSTWDTMO         ! timeout in milliseconds
GLOBAL INT HOSTWDFIRED       ! 1 = this program killed motion

INT last
REAL deadline

! Start disarmed. The host arms only after its heartbeat is running, so
! loading this buffer can never kill motion on its own.
HOSTWDEN = 0
HOSTWDFIRED = 0

IF HOSTWDTMO <= 0
  HOSTWDTMO = 1000
END

last = HOSTWDOG
deadline = TIME + HOSTWDTMO

WHILE 1

  ! Heartbeat seen -- push the deadline out.
  IF HOSTWDOG <> last
    last = HOSTWDOG
    deadline = TIME + HOSTWDTMO
  END

  IF HOSTWDEN
    IF TIME > deadline
      ! Reason 9001 lands in MERR so the cause is visible afterwards.
      KILL (0,1,4,5,6), 9001
      HOSTWDFIRED = 1
      ! Disarm so this fires once per host loss rather than every cycle.
      HOSTWDEN = 0
    END
  END

  ! 20 ms granularity against a timeout that is 1 s by default.
  WAIT 20

END

STOP