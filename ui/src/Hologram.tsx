import { useEffect, useRef } from "react";

export type CoreState = "idle" | "listening" | "thinking" | "speaking" | "error";

const PALETTE: Record<CoreState, { a: string; b: string; rgb: [number, number, number] }> = {
  idle: { a: "#36e6ff", b: "#0a7ea4", rgb: [54, 230, 255] },
  listening: { a: "#5dffd0", b: "#16a085", rgb: [93, 255, 208] },
  thinking: { a: "#ffd166", b: "#e08e0b", rgb: [255, 209, 102] },
  speaking: { a: "#9ad8ff", b: "#3aa0ff", rgb: [154, 216, 255] },
  error: { a: "#ff6b6b", b: "#c0392b", rgb: [255, 107, 107] },
};

// A reactive holographic core: pulsing orb, rotating arc-rings, orbiting motes.
// `level` (0..1) drives the pulse from mic input / TTS output.
export default function Hologram({ state, level = 0 }: { state: CoreState; level?: number }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const stateRef = useRef(state);
  const levelRef = useRef(level);
  stateRef.current = state;
  levelRef.current = level;

  useEffect(() => {
    const canvas = ref.current!;
    const ctx = canvas.getContext("2d")!;
    let raf = 0;
    let t = 0;
    let smooth = 0; // smoothed level

    const resize = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const size = Math.min(canvas.clientWidth, canvas.clientHeight);
      canvas.width = size * dpr;
      canvas.height = size * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);

    const draw = () => {
      const size = Math.min(canvas.clientWidth, canvas.clientHeight);
      const cx = size / 2;
      const cy = size / 2;
      const R = size * 0.34;
      const pal = PALETTE[stateRef.current];
      smooth += (levelRef.current - smooth) * 0.2;
      const speedBoost = stateRef.current === "thinking" ? 2.2 : 1;
      t += 0.016 * speedBoost;

      ctx.clearRect(0, 0, size, size);
      ctx.globalCompositeOperation = "lighter";

      // ambient glow
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 2.2);
      glow.addColorStop(0, `rgba(${pal.rgb.join(",")},0.16)`);
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, size, size);

      // rotating arc-rings
      const rings = [
        { r: R * 1.05, w: 2.5, span: 1.7, dir: 1, sp: 0.6 },
        { r: R * 1.28, w: 1.5, span: 1.1, dir: -1, sp: 0.35 },
        { r: R * 1.5, w: 1, span: 0.6, dir: 1, sp: 0.22 },
      ];
      for (const ring of rings) {
        const segs = 3;
        for (let s = 0; s < segs; s++) {
          const start = t * ring.sp * ring.dir + (s * Math.PI * 2) / segs;
          ctx.beginPath();
          ctx.arc(cx, cy, ring.r, start, start + ring.span);
          ctx.strokeStyle = `rgba(${pal.rgb.join(",")},0.65)`;
          ctx.lineWidth = ring.w;
          ctx.shadowColor = pal.a;
          ctx.shadowBlur = 12;
          ctx.stroke();
        }
      }
      ctx.shadowBlur = 0;

      // reticle ticks
      ctx.strokeStyle = `rgba(${pal.rgb.join(",")},0.35)`;
      ctx.lineWidth = 1;
      for (let i = 0; i < 60; i++) {
        const ang = (i / 60) * Math.PI * 2 - t * 0.1;
        const inner = R * 1.62;
        const outer = inner + (i % 5 === 0 ? 10 : 4);
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(ang) * inner, cy + Math.sin(ang) * inner);
        ctx.lineTo(cx + Math.cos(ang) * outer, cy + Math.sin(ang) * outer);
        ctx.stroke();
      }

      // orbiting motes
      for (let i = 0; i < 14; i++) {
        const ang = t * (0.4 + (i % 5) * 0.08) * (i % 2 ? 1 : -1) + i;
        const orb = R * (0.7 + ((i * 37) % 90) / 100);
        const x = cx + Math.cos(ang) * orb;
        const y = cy + Math.sin(ang) * orb;
        ctx.beginPath();
        ctx.arc(x, y, 1.6, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${pal.rgb.join(",")},0.8)`;
        ctx.fill();
      }

      // pulsing core
      const pulse = 1 + Math.sin(t * 2) * 0.05 + smooth * 0.45;
      const coreR = R * 0.5 * pulse;
      const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      core.addColorStop(0, "rgba(255,255,255,0.95)");
      core.addColorStop(0.25, pal.a);
      core.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fill();

      // inner spinning ring around core
      ctx.beginPath();
      ctx.arc(cx, cy, R * 0.62, t * 1.2, t * 1.2 + 4.6);
      ctx.strokeStyle = `rgba(255,255,255,0.5)`;
      ctx.lineWidth = 2;
      ctx.stroke();

      ctx.globalCompositeOperation = "source-over";
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return <canvas ref={ref} className="hologram-canvas" />;
}
