"use client";

import { useEffect, useRef } from "react";

/** A small, dependency-free projected torus. Decorative; all state is in the DOM. */
export function NeuralCore() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let width = 0,
      height = 0,
      frame = 0,
      rotation = 0;
    let visible = true;
    const draw = () => {
      context.clearRect(0, 0, width, height);
      const size = Math.min(width, height) * 0.3;
      const angle = rotation + 0.5;
      const project = (u: number, v: number) => {
        const ripple = 0.06 * Math.sin(u * 3 + rotation * 2);
        const radius = 0.44 + ripple;
        const x = (1 + radius * Math.cos(v)) * Math.cos(u);
        const y = (1 + radius * Math.cos(v)) * Math.sin(u);
        const z = radius * Math.sin(v);
        const xx = x * Math.cos(angle) - z * Math.sin(angle);
        const zz = x * Math.sin(angle) + z * Math.cos(angle);
        const yy = y * Math.cos(0.85) - zz * Math.sin(0.85);
        const depth = y * Math.sin(0.85) + zz * Math.cos(0.85);
        const perspective = 4.5 / (4.5 - depth);
        return [
          width * 0.52 + xx * size * perspective,
          height * 0.49 + yy * size * perspective,
          depth,
        ];
      };
      for (let ring = 0; ring < 62; ring++) {
        const u = (ring / 62) * Math.PI * 2;
        const depth = project(u, 0)[2];
        context.beginPath();
        for (let step = 0; step <= 68; step++) {
          const p = project(u, (step / 68) * Math.PI * 2);
          if (!step) context.moveTo(p[0], p[1]);
          else context.lineTo(p[0], p[1]);
        }
        const intensity = (depth + 1.6) / 3.2;
        context.strokeStyle = `rgba(${Math.round(133 + intensity * 80)}, ${Math.round(91 + intensity * 85)}, 255, ${0.18 + intensity * 0.52})`;
        context.lineWidth = 0.75 + intensity * 0.45;
        context.stroke();
      }
      for (let ring = 0; ring < 14; ring++) {
        context.beginPath();
        for (let step = 0; step <= 120; step++) {
          const p = project(
            (step / 120) * Math.PI * 2,
            (ring / 14) * Math.PI * 2,
          );
          if (!step) context.moveTo(p[0], p[1]);
          else context.lineTo(p[0], p[1]);
        }
        context.strokeStyle = "rgba(184, 157, 255, 0.25)";
        context.lineWidth = 0.6;
        context.stroke();
      }
    };
    let last = 0;
    const animate = (now: number) => {
      if (visible && !document.hidden && now - last > 33) {
        rotation += 0.0025;
        draw();
        last = now;
      }
      if (!motion.matches) frame = requestAnimationFrame(animate);
    };
    const resize = () => {
      const box = canvas.getBoundingClientRect();
      width = box.width;
      height = box.height;
      const scale = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = width * scale;
      canvas.height = height * scale;
      context.setTransform(scale, 0, 0, scale, 0, 0);
      draw();
    };
    const resizeObserver = new ResizeObserver(resize);
    const intersection = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
    });
    resizeObserver.observe(canvas);
    intersection.observe(canvas);
    resize();
    const updateMotion = () => {
      cancelAnimationFrame(frame);
      draw();
      if (!motion.matches) frame = requestAnimationFrame(animate);
    };
    motion.addEventListener("change", updateMotion);
    updateMotion();
    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      intersection.disconnect();
      motion.removeEventListener("change", updateMotion);
    };
  }, []);
  return (
    <div className="neural-art" aria-hidden="true">
      <div className="core-aura" />
      <canvas ref={ref} />
      <span className="orb-point point-one" />
      <span className="orb-point point-two" />
      <span className="orb-caption">CONNECTED INTELLIGENCE</span>
      <span className="orb-coordinate">F / 01</span>
    </div>
  );
}
