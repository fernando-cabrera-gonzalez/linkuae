; Pulse: a square crossing barriers in bursts. The whole game fits in the boot sector.
; It takes over the machine and draws almost everything with hardware sprites: the square is
; sprite 0 and each barrier is one sprite reused twice vertically, above and below its gap.
; The course never changes: the seed is fixed and depends on neither time nor chance.
; Button: joystick fire (port 2) or left mouse button.

CUSTOM		equ	$dff000				; base of the custom chip registers
CIAA_PRA	equ	$bfe001				; CIA-A port A: fire and mouse button

; Memory, all of it in the first 512 KB of chip RAM
STATE		equ	$60000				; public state, read by bot.py
COPPER		equ	$60100				; copper list, copied here at boot
SPR_PLAYER	equ	$61000				; sprite data of the square
SPR_NULL	equ	$61f00				; empty sprite for the unused channels
SPR_GATES	equ	$62000				; sprite data of the three barriers
GATE_BYTES	equ	$800				; room for each barrier's sprite data
PLANE		equ	$65000				; bitplane: sprites only show on lines with one
STACK		equ	$7f000				; our own stack, the OS is gone

; Public state (see bot.py): addresses and order are part of the demo
ST_SIG		equ	0				; long: 'PLSE' once the game is ready
ST_MODE		equ	4				; 0 waiting for the button, 1 playing, 2 game over
ST_SCORE	equ	6				; barriers passed in this game
ST_Y		equ	8				; square y in pixels
ST_VY		equ	10				; speed in 1/256 pixel per frame
ST_GATE_X	equ	12				; 3 words: x of each barrier
ST_GATE_GAP	equ	18				; 3 words: top y of each gap
ST_BEST		equ	24				; best score since boot
ST_FIXY		equ	26				; y with 8 fraction bits
ST_SEED		equ	28				; state of the course generator
ST_WAS_DOWN	equ	30				; button state on the last frame
ST_WAIT		equ	32				; frames left before a restart is accepted

PLAYER_X	equ	64				; the square never moves horizontally
PLAYER_H	equ	12				; square height in lines
GATE_W		equ	16				; width of a sprite, and of a barrier
GATE_GAP	equ	72				; height of each gap
GATE_SPACING	equ	130				; pixels between barriers
SPEED		equ	2				; pixels per frame the barriers move
GRAVITY		equ	24				; 1/256 pixel per frame^2
FLAP		equ	-640				; speed set by a button press
TOP		equ	$2c				; first visible line
SCREEN_H	equ	256				; visible lines
SCREEN_W	equ	320				; visible pixels per line
PLANE_BYTES	equ	40*SCREEN_H			; size of the bitplane
DIGITS_AT	equ	8*40+18				; offset of the score in the plane

	lea	CUSTOM,a5				; a5 stays on the custom registers
	lea	STATE,a4				; a4 stays on the game state
	lea	STACK,sp				; leave the boot stack behind
	move.w	#$7fff,d0				; mask that clears every bit
	move.w	d0,$9a(a5)				; INTENA: no interrupts, the machine is ours
	move.w	d0,$9c(a5)				; INTREQ: none pending
	move.w	d0,$96(a5)				; DMACON: no DMA while setting up

	lea	copper_list(pc),a0			; the list, inside this boot sector
	lea	COPPER,a1				; where the copper will read it
	moveq	#(copper_end-copper_list)/4-1,d0	; instructions to copy
.copy	move.l	(a0)+,(a1)+				; copy one instruction
	dbf	d0,.copy				; next

	lea	PLANE,a0				; the empty bitplane
	move.w	#PLANE_BYTES/4-1,d0			; longs to clear
.clear	clr.l	(a0)+					; clear one
	dbf	d0,.clear				; next

	lea	SPR_PLAYER+4,a0				; square lines, after its control words
	moveq	#PLAYER_H-1,d0				; lines to fill
.body	move.l	#$ffff0000,(a0)+			; solid line in the sprite's colour 1
	dbf	d0,.body				; next
	clr.l	(a0)					; end of the square's sprite data
	clr.l	SPR_NULL				; an empty sprite for the unused channels

	move.w	#$2c81,$8e(a5)				; DIWSTRT: 320x256 window
	move.w	#$2cc1,$90(a5)				; DIWSTOP
	move.w	#$38,$92(a5)				; DDFSTRT: standard low resolution fetch
	move.w	#$d0,$94(a5)				; DDFSTOP
	clr.w	$108(a5)				; BPL1MOD: no modulo
	move.w	#$0024,$104(a5)				; BPLCON2: sprites in front of the bitplane
	move.w	#$0fff,$182(a5)				; COLOR01: digits
	move.w	#$02d8,$1aa(a5)				; COLOR21: barrier in sprite 2
	move.w	#$02d8,$1b2(a5)				; COLOR25: barrier in sprite 4
	move.w	#$02d8,$1ba(a5)				; COLOR29: barrier in sprite 6
	move.l	#COPPER,$80(a5)				; COP1LC: our copper list
	clr.w	$88(a5)					; COPJMP1: restart the copper there

	clr.w	ST_BEST(a4)				; no best score yet
	clr.b	ST_WAS_DOWN(a4)				; button not held
	bsr	new_game				; prepare the course
	move.l	#'PLSE',ST_SIG(a4)			; last: the state is ready to read
	bra	draw					; set the sprites, DMA goes on after that

; One pass per frame, right after the visible area: the sprites have been fetched by then
main	move.w	#TOP+SCREEN_H+1,d3			; line that starts each pass
.leave	bsr	line					; read the beam
	cmp.w	d3,d0					; still on that line?
	beq	.leave					; then wait to leave it, so as not to run twice
.reach	bsr	line					; read the beam
	cmp.w	d3,d0					; back on it?
	bne	.reach					; not yet

	move.b	CIAA_PRA,d0				; fire and button are active low
	not.b	d0					; now active high
	and.b	#$c0,d0					; keep the two buttons
	sne	d0					; d0.b = down
	move.b	ST_WAS_DOWN(a4),d1			; state on the last frame
	move.b	d0,ST_WAS_DOWN(a4)			; remember it for the next one
	not.b	d1					; was up...
	and.b	d0,d1					; ...and is down: d1.b = just pressed

	move.w	ST_MODE(a4),d0				; which mode?
	beq	waiting					; 0: waiting for the button
	subq.w	#1,d0					; next mode
	beq	playing					; 1: playing
	tst.w	ST_WAIT(a4)				; 2: game over. Is the pause finished?
	beq	.ready					; yes
	subq.w	#1,ST_WAIT(a4)				; one frame less
	bra	draw					; nothing else during the pause
.ready	tst.b	d1					; just pressed?
	beq	draw					; no: keep waiting
	bsr	new_game				; yes: back to the start
	bra	draw					; the shapes changed: place the sprites at once

line	move.l	4(a5),d0				; VPOSR and VHPOSR: current beam line
	lsr.l	#8,d0					; drop the horizontal position
	and.w	#$1ff,d0				; keep the 9 bits of the line
	rts						; return it in d0

waiting	tst.b	d1					; just pressed?
	beq	draw					; no: nothing moves
	move.w	#1,ST_MODE(a4)				; yes: the game starts, with this press

playing	tst.b	d1					; just pressed?
	beq	.fall					; no
	move.w	#FLAP,ST_VY(a4)				; yes: an upward burst
.fall	add.w	#GRAVITY,ST_VY(a4)			; gravity, always
	move.w	ST_FIXY(a4),d0				; current y...
	add.w	ST_VY(a4),d0				; ...plus the speed
	move.w	d0,ST_FIXY(a4)				; new y with fraction
	lsr.w	#8,d0					; unsigned: above the top wraps to 255
	move.w	d0,ST_Y(a4)				; publish it in pixels
	cmp.w	#SCREEN_H-PLAYER_H,d0			; hit the floor or went over the top?
	bhi	crash					; unsigned: it catches both

	moveq	#2,d7					; the three barriers
.gate	move.w	d7,d6					; barrier number...
	add.w	d6,d6					; ...as an offset into word arrays
	move.w	ST_GATE_X(a4,d6.w),d2			; its x
	subq.w	#SPEED,d2				; moves left
	cmp.w	#-GATE_W,d2				; completely off the left edge?
	bgt	.same					; no
	add.w	#3*GATE_SPACING,d2			; yes: back behind the other two
	bsr	new_gap					; with a new gap
.same	move.w	d2,ST_GATE_X(a4,d6.w)			; store its x
	cmp.w	#PLAYER_X-GATE_W,d2			; just left behind?
	bne	.touch					; no
	addq.w	#1,ST_SCORE(a4)				; one more point
	bsr	show_score				; and show it
.touch	cmp.w	#PLAYER_X+GATE_W,d2			; still to the right of the square?
	bge	.next					; then no contact
	cmp.w	#PLAYER_X-GATE_W,d2			; already to the left of it?
	ble	.next					; then no contact
	move.w	ST_Y(a4),d0				; overlapping in x: check the square's y
	move.w	ST_GATE_GAP(a4,d6.w),d1			; top of the gap
	cmp.w	d1,d0					; above it?
	blt	crash					; hit the top piece
	add.w	#GATE_GAP-PLAYER_H,d1			; lowest y that still fits
	cmp.w	d1,d0					; below it?
	bgt	crash					; hit the bottom piece
.next	dbf	d7,.gate				; next barrier
	bra	draw					; all clear

crash	move.w	#2,ST_MODE(a4)				; game over
	move.w	#50,ST_WAIT(a4)				; one second before accepting a restart
	move.w	#$0f30,$1a2(a5)				; COLOR17: the square turns red
	move.w	ST_SCORE(a4),d0				; score of this game
	cmp.w	ST_BEST(a4),d0				; better than the best?
	bls	draw					; no
	move.w	d0,ST_BEST(a4)				; yes: new best

; Only the control words change: each sprite's shape is already in memory
draw	move.w	ST_Y(a4),d0				; square y on screen...
	add.w	#TOP,d0					; ...as a beam line: first line
	move.w	d0,d1					; same...
	add.w	#PLAYER_H,d1				; ...plus its height: end line
	move.w	#PLAYER_X,d2				; fixed x
	lea	SPR_PLAYER,a0				; its sprite
	bsr	sprite_pos				; write its control words

	moveq	#2,d7					; the three barriers
.gate	move.w	d7,d6					; barrier number...
	add.w	d6,d6					; ...as an offset into word arrays
	move.w	ST_GATE_GAP(a4,d6.w),d4			; top of its gap: lines of the top piece
	bsr	gate_addr				; a0 = its sprite
	move.w	ST_GATE_X(a4,d6.w),d2			; its x
	move.w	#TOP,d0					; top piece: from the first line...
	move.w	d4,d1					; ...for as many lines as the gap's y...
	add.w	d0,d1					; ...gives its end line
	bsr	sprite_pos				; write its control words
	move.w	d4,d0					; lines of the top piece...
	lsl.w	#2,d0					; ...at 4 bytes per line...
	lea	4(a0,d0.w),a0				; ...skip them: the bottom piece's control words
	move.w	ST_GATE_X(a4,d6.w),d2			; x again, sprite_pos changed it
	move.w	#TOP+GATE_GAP,d0			; bottom piece: first line below the gap...
	add.w	d4,d0					; ...counting from the gap's top
	move.w	#TOP+SCREEN_H,d1			; down to the bottom of the screen
	bsr	sprite_pos				; write its control words
	dbf	d7,.gate				; next barrier
	move.w	#$83a0,$96(a5)				; DMA on, now that the sprites are set
	bra	main					; next frame

; a0 = sprite, d0 = first line, d1 = end line, d2 = x. Keeps a0.
sprite_pos
	cmp.w	#SCREEN_W+GATE_W,d2			; HSTART has 9 bits: further right wraps into view
	ble	.fits					; this one fits
	move.w	#SCREEN_W+GATE_W,d2			; wait just outside the screen
.fits	add.w	#$80,d2					; x as a horizontal beam position
	move.w	d0,d3					; first line...
	lsl.w	#8,d3					; ...V7-0 in the high byte
	move.w	d2,d5					; x...
	lsr.w	#1,d5					; ...H8-1...
	move.b	d5,d3					; ...in the low byte
	move.w	d3,(a0)					; SPRxPOS
	move.w	d1,d3					; end line...
	lsl.w	#8,d3					; ...V7-0 in the high byte
	lsr.w	#8,d0					; first line's V8...
	lsl.w	#2,d0					; ...goes to bit 2
	or.b	d0,d3					; add it
	lsr.w	#8,d1					; end line's V8...
	add.w	d1,d1					; ...goes to bit 1
	or.b	d1,d3					; add it
	and.w	#1,d2					; x's H0...
	or.b	d2,d3					; ...goes to bit 0
	move.w	d3,2(a0)				; SPRxCTL
	rts						; done

; d7 = barrier. Returns a0 = its sprite.
gate_addr
	move.w	d7,d3					; barrier number...
	mulu	#GATE_BYTES,d3				; ...as an offset
	lea	SPR_GATES,a0				; first barrier's sprite
	add.l	d3,a0					; this one's
	rts						; done

; New gap for barrier d7 (d6 = d7*2), and its shape: top lines + bottom lines + end marker
new_gap	move.w	ST_SEED(a4),d0				; 16-bit LFSR: same sequence on every game
	lsr.w	#1,d0					; shift
	bcc	.even					; no bit out: no feedback
	eor.w	#$b400,d0				; feedback
.even	move.w	d0,ST_SEED(a4)				; keep it for the next gap
	and.w	#63,d0					; 64 possible gaps...
	add.w	#52,d0					; ...from 52 to 115: the next is always reachable
	move.w	d0,ST_GATE_GAP(a4,d6.w)			; publish it
	bsr	gate_addr				; a0 = its sprite
	addq.l	#4,a0					; skip the top piece's control words
	move.w	d0,d1					; its height is the gap's y
	bsr	.fill					; write those lines
	addq.l	#4,a0					; skip the bottom piece's control words
	move.w	#SCREEN_H-GATE_GAP,d1			; what the gap leaves...
	sub.w	d0,d1					; ...minus the top piece
	bsr	.fill					; write those lines
	clr.l	(a0)					; end of this sprite's data
	rts						; done
.fill	subq.w	#1,d1					; d1 lines, for dbf
.row	move.l	#$ffff0000,(a0)+			; solid line in the sprite's colour 1
	dbf	d1,.row					; next
	rts						; done

new_game
	move.w	#$ace1,ST_SEED(a4)			; fixed course
	clr.l	ST_MODE(a4)				; mode and score at 0
	move.w	#(SCREEN_H-PLAYER_H)/2,d0		; square in the middle
	move.w	d0,ST_Y(a4)				; in pixels
	lsl.w	#8,d0					; and with fraction
	move.w	d0,ST_FIXY(a4)				; store it
	clr.w	ST_VY(a4)				; not moving
	clr.w	ST_WAIT(a4)				; no pause
	move.w	#$0fd0,$1a2(a5)				; COLOR17: yellow square
	moveq	#2,d7					; the three barriers
.gate	move.w	d7,d6					; barrier number...
	add.w	d6,d6					; ...as an offset into word arrays
	move.w	d7,d2					; each one further away...
	mulu	#GATE_SPACING,d2			; ...by the spacing...
	add.w	#SCREEN_W,d2				; ...from the right edge
	move.w	d2,ST_GATE_X(a4,d6.w)			; store its x
	bsr	new_gap					; its gap and shape
	dbf	d7,.gate				; next barrier
	; falls through to show_score

; Three 8x10 digits drawn in the bitplane
show_score
	movem.l	d0-d2/d6-d7/a0-a1,-(sp)			; called from the loops: keep their registers
	moveq	#0,d0					; clear the high word for divu
	move.w	ST_SCORE(a4),d0				; the score
	moveq	#2,d7					; three digits, from the right
.digit	divu	#10,d0					; remainder:quotient
	swap	d0					; quotient:remainder
	move.w	d0,d1					; d1 = the digit
	clr.w	d0					; drop it...
	swap	d0					; ...d0 = what is left
	mulu	#5,d1					; 5 bytes per font digit
	lea	font(pc,d1.w),a0			; this digit's rows
	lea	PLANE+DIGITS_AT,a1			; where the score goes
	add.w	d7,a1					; this digit's column
	moveq	#4,d2					; five rows
.row	move.b	(a0),(a1)				; first of the row's two lines
	move.b	(a0)+,40(a1)				; second line
	lea	80(a1),a1				; next row
	dbf	d2,.row					; next
	dbf	d7,.digit				; next digit
	movem.l	(sp)+,d0-d2/d6-d7/a0-a1			; restore
	rts						; done

font	dc.b	$fc,$cc,$cc,$cc,$fc			; 0
	dc.b	$30,$30,$30,$30,$30			; 1
	dc.b	$fc,$0c,$fc,$c0,$fc			; 2
	dc.b	$fc,$0c,$3c,$0c,$fc			; 3
	dc.b	$cc,$cc,$fc,$0c,$0c			; 4
	dc.b	$fc,$c0,$fc,$0c,$fc			; 5
	dc.b	$fc,$c0,$fc,$cc,$fc			; 6
	dc.b	$fc,$0c,$0c,$0c,$0c			; 7
	dc.b	$fc,$cc,$fc,$cc,$fc			; 8
	dc.b	$fc,$cc,$fc,$0c,$fc			; 9

	even						; the copper list needs an even address
copper_list
	dc.w	$120,SPR_PLAYER>>16			; SPR0PT: the square
	dc.w	$122,SPR_PLAYER&$ffff
	dc.w	$124,SPR_NULL>>16			; SPR1PT: unused
	dc.w	$126,SPR_NULL&$ffff
	dc.w	$128,SPR_GATES>>16			; SPR2PT: first barrier
	dc.w	$12a,SPR_GATES&$ffff
	dc.w	$12c,SPR_NULL>>16			; SPR3PT: unused
	dc.w	$12e,SPR_NULL&$ffff
	dc.w	$130,(SPR_GATES+GATE_BYTES)>>16		; SPR4PT: second barrier
	dc.w	$132,(SPR_GATES+GATE_BYTES)&$ffff
	dc.w	$134,SPR_NULL>>16			; SPR5PT: unused
	dc.w	$136,SPR_NULL&$ffff
	dc.w	$138,(SPR_GATES+2*GATE_BYTES)>>16	; SPR6PT: third barrier
	dc.w	$13a,(SPR_GATES+2*GATE_BYTES)&$ffff
	dc.w	$13c,SPR_NULL>>16			; SPR7PT: unused
	dc.w	$13e,SPR_NULL&$ffff
	dc.w	$e0,PLANE>>16				; BPL1PT: the bitplane
	dc.w	$e2,PLANE&$ffff
	dc.w	$100,$1200				; BPLCON0: one bitplane, so sprites show everywhere
	dc.w	$180,$012				; COLOR00: top of the background gradient
	dc.w	$6c01,$fffe				; wait for line $6c
	dc.w	$180,$013				; a little bluer
	dc.w	$ac01,$fffe				; wait for line $ac
	dc.w	$180,$014				; bluer
	dc.w	$ec01,$fffe				; wait for line $ec
	dc.w	$180,$015				; bluest
	dc.w	$ffff,$fffe				; end of the list
copper_end
