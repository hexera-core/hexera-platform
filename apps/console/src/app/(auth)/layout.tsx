import { AuthStyles } from "@/app/_components/legacy-styles";
import { AuthChrome } from "@/app/_components/site-chrome";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <AuthStyles />
      <AuthChrome>{children}</AuthChrome>
    </>
  );
}
