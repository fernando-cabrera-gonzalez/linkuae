"""The planner's per-tick costs and the backward pass, compiled with Numba.

Every value goes through the same IEEE operations, in the same order and with the same types (float32 or float64) as
the numpy code in bot.py (used when Numba is not installed): the same algorithm, compiled.
No fastmath: nothing is reordered, fused or approximated.
"""
import numpy as np
from numba import njit

F4 = np.float32


@njit(cache=False, nogil=True)
def _mark(o, has9, t, kf, shiftT, CX, CY, add_x, add_y, rows_ok, out, sxi, syi, sdyi):
    """out[y, x] |= ship's box overlaps any row of o at tick t. add_x/add_y: [N] the extra half sizes (float32)."""
    N = o.shape[0]
    NX, NY = CX.shape[0], CY.shape[0]
    colm = np.empty(NX, np.bool_)
    for n in range(N):
        if rows_ok is not None and not rows_ok[n]:
            continue
        if has9:
            sx_, sy_, sdy = o[n, 6], o[n, 7], o[n, 8]
        else:
            sx_, sy_, sdy = sxi, syi, sdyi
        thx = (o[n, 4] + sx_) + add_x[n]
        thy = (o[n, 5] + sy_) + add_y[n]
        xo = o[n, 0] + o[n, 2] * kf[t]
        yo = (o[n, 1] + o[n, 3] * kf[t]) - sdy
        anyc = False
        for x in range(NX):
            c = abs((xo - shiftT[t, x]) - CX[x]) < thx
            colm[x] = c
            anyc |= c
        if not anyc:
            continue
        for y in range(NY):
            if abs(yo - CY[y]) < thy:
                for x in range(NX):
                    if colm[x]:
                        out[y, x] = True


@njit(cache=False, nogil=True)
def plan(aimrun, term_aim, wall, kf, shiftT, CX, CY,
         hard, vx, vy, gT, near_px, hitW, nearW,
         shooters, prox_w,
         grounds, g_margin, g_w,
         unwanted, u_x, u_y, u_w,
         wanted, lowT, w_x, w_y, wW, apply_wanted,
         ship_hit_x, ship_hit, ship_hit_dy, move_cost, TE, iy, ix):
    T, NX, NY = kf.shape[0], CX.shape[0], CY.shape[0]
    Nh, Ns, Ng, Nu, Nw = hard.shape[0], shooters.shape[0], grounds.shape[0], unwanted.shape[0], wanted.shape[0]
    zero32 = np.float32(0.0)
    one32 = np.float32(1.0)
    cost = np.empty((T, NY, NX), np.float32)
    exact = np.zeros((TE, NY, NX), np.bool_)
    hit = np.zeros((NY, NX), np.bool_)
    ring = np.zeros((NY, NX), np.bool_)
    prox = np.zeros((NY, NX), np.float32)
    flag = np.zeros((NY, NX), np.bool_)
    ex = np.empty(NX, np.float32)
    ey = np.empty(NY, np.float32)
    ax0 = np.empty(Nh, np.float32)
    ay0 = np.empty(Nh, np.float32)
    ax1 = np.empty(Nh, np.float32)
    ay1 = np.empty(Nh, np.float32)
    ax2 = np.empty(Nh, np.float32)
    ay2 = np.empty(Nh, np.float32)
    for n in range(Nh):
        ax0[n] = np.float32(0.5) * vx[n]
        ay0[n] = np.float32(0.5) * vy[n]
    gx = np.empty(Ng, np.float32)
    gx[:] = g_margin
    ux = np.empty(Nu, np.float32)
    ux[:] = u_x
    uy = np.empty(Nu, np.float32)
    uy[:] = u_y
    wx = np.empty(Nw, np.float32)
    wx[:] = w_x
    wy = np.empty(Nw, np.float32)
    wy[:] = w_y
    c64 = np.empty((NY, NX), np.float64)       # the cost while numpy would have it in float64
    c32 = np.empty((NY, NX), np.float32)       # ... and while it would still be float32
    danger = 0.0
    for t in range(T):
        is64 = False
        for y in range(NY):
            for x in range(NX):
                c32[y, x] = aimrun[y, x]
        thr_cell = 0.0
        thr_set = False
        if Nh:
            hit[:] = False
            ring[:] = False
            for n in range(Nh):
                ax1[n] = vx[n] + gT[t]
                ay1[n] = vy[n] + gT[t]
                ax2[n] = (vx[n] + gT[t]) + near_px
                ay2[n] = (vy[n] + gT[t]) + near_px
            if t < TE:
                _mark(hard, True, t, kf, shiftT, CX, CY, ax0, ay0, None, exact[t], zero32, zero32, zero32)
            _mark(hard, True, t, kf, shiftT, CX, CY, ax1, ay1, None, hit, zero32, zero32, zero32)
            _mark(hard, True, t, kf, shiftT, CX, CY, ax2, ay2, None, ring, zero32, zero32, zero32)
            hw, nw = hitW[t], nearW[t]
            for y in range(NY):
                for x in range(NX):
                    h = hit[y, x]
                    nr = nw if (ring[y, x] and not h) else 0.0
                    c64[y, x] = (np.float64(c32[y, x]) + (hw if h else 0.0)) + nr
            is64 = True
            thr_cell = 0.0 + (nw if (ring[iy, ix] and not hit[iy, ix]) else 0.0)
            thr_set = True
        if Ns:
            prox[:] = zero32
            for j in range(Ns):                # one shooter after another, as numpy summed them
                R = shooters[j, 6]
                lim = R * np.float32(1.01)     # beyond this its term is exactly 0: adding it changes nothing
                xo = shooters[j, 0] + shooters[j, 2] * kf[t]
                yo = shooters[j, 1] + shooters[j, 3] * kf[t]
                for x in range(NX):
                    v = abs((xo - shiftT[t, x]) - CX[x]) - shooters[j, 4]
                    ex[x] = v if v > zero32 else zero32
                for y in range(NY):
                    v = abs(yo - CY[y]) - shooters[j, 5]
                    ey[y] = v if v > zero32 else zero32
                for y in range(NY):
                    if ey[y] > lim:
                        continue
                    ey2 = ey[y] * ey[y]
                    for x in range(NX):
                        if ex[x] > lim:
                            continue
                        d = np.sqrt(ex[x] * ex[x] + ey2)
                        u = one32 - d / R
                        if u > zero32:
                            prox[y, x] = prox[y, x] + prox_w * (u * u)
            if is64:
                for y in range(NY):
                    for x in range(NX):
                        c64[y, x] = c64[y, x] + np.float64(prox[y, x])
                thr_cell = thr_cell + np.float64(prox[iy, ix])
            else:
                for y in range(NY):
                    for x in range(NX):
                        c32[y, x] = c32[y, x] + prox[y, x]
                thr_cell = np.float64(zero32 + prox[iy, ix])
            thr_set = True
        if thr_set and thr_cell > danger:
            danger = thr_cell
        if Ng:
            flag[:] = False
            _mark(grounds, True, t, kf, shiftT, CX, CY, gx, gx, None, flag, zero32, zero32, zero32)
            if not is64:
                for y in range(NY):
                    for x in range(NX):
                        c64[y, x] = np.float64(c32[y, x])
                is64 = True
            for y in range(NY):
                for x in range(NX):
                    c64[y, x] = c64[y, x] + (g_w if flag[y, x] else 0.0)
        if Nu:
            flag[:] = False
            _mark(unwanted, False, t, kf, shiftT, CX, CY, ux, uy, None, flag, ship_hit_x, ship_hit, ship_hit_dy)
            if not is64:
                for y in range(NY):
                    for x in range(NX):
                        c64[y, x] = np.float64(c32[y, x])
                is64 = True
            for y in range(NY):
                for x in range(NX):
                    c64[y, x] = c64[y, x] + (u_w if flag[y, x] else 0.0)
        if Nw and apply_wanted:
            flag[:] = False
            _mark(wanted, False, t, kf, shiftT, CX, CY, wx, wy, lowT[t], flag, ship_hit_x, ship_hit, ship_hit_dy)
            if not is64:
                for y in range(NY):
                    for x in range(NX):
                        c64[y, x] = np.float64(c32[y, x])
                is64 = True
            ww = wW[t]
            for y in range(NY):
                for x in range(NX):
                    c64[y, x] = c64[y, x] - (ww if flag[y, x] else 0.0)
        for y in range(NY):
            for x in range(NX):
                cost[t, y, x] = np.float32(c64[y, x]) if is64 else c32[y, x]
    # the wall, scaled by the danger (numpy: float32 wall times the Python float danger -> float32)
    dng = min(1.0, danger / np.float64(prox_w))
    d32 = np.float32(dng)
    wd = np.empty(NX, np.float32)
    for x in range(NX):
        wd[x] = wall[x] * d32
    for t in range(T):
        for y in range(NY):
            for x in range(NX):
                cost[t, y, x] = cost[t, y, x] + wd[x]
    # backwards: the cheapest way on from every cell (the 8 neighbours, in the same order as the numpy code)
    v = term_aim.copy()
    tmp = np.empty((NY, NX), np.float32)
    out = np.empty((NY, NX), np.float32)
    for t in range(T - 1, 0, -1):
        for y in range(NY):
            for x in range(NX):
                tmp[y, x] = cost[t, y, x] + v[y, x]
        for y in range(NY):
            for x in range(NX):
                m = tmp[y, x]
                for dy in range(-1, 2):
                    yy = y + dy
                    if yy < 0 or yy >= NY:
                        continue
                    for dx in range(-1, 2):
                        xx = x + dx
                        if (dx == 0 and dy == 0) or xx < 0 or xx >= NX:
                            continue
                        w = tmp[yy, xx] + move_cost
                        if w < m:
                            m = w
                out[y, x] = m
        v, out = out, v
    q = np.empty((NY, NX), np.float32)
    for y in range(NY):
        for x in range(NX):
            q[y, x] = cost[0, y, x] + v[y, x]
    return cost, exact, q, dng
