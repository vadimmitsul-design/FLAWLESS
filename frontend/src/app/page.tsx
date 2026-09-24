import { Cabinet } from "@/components/cabinet";

export default function Page() {
  return <Cabinet demo={process.env.NEXT_PUBLIC_CABINET_DEMO === "true"} />;
}
