import { HealthCheckBanner } from "@/components/health/healthcheck";
import { User } from "@/lib/types";
import {
  getCurrentUserSS,
  getAuthUrlSS,
  getAuthTypeMetadataSS,
  AuthTypeMetadata,
} from "@/lib/userSS";
import { redirect } from "next/navigation";
import { SignInButton } from "./SignInButton";
import { EmailPasswordForm } from "./EmailPasswordForm";
import { Card, Title, Text } from "@tremor/react";
import Link from "next/link";
import { Logo } from "@/components/Logo";
import { LoginText } from "./LoginText";
import { getSecondsUntilExpiration } from "@/lib/time";
import { headers } from 'next/headers';
import { HeaderLoginLoading } from "./HeaderLogin";

const Page = async ({
  searchParams,
}: {
  searchParams?: { [key: string]: string | string[] | undefined };
}) => {
  const autoRedirectDisabled = searchParams?.disableAutoRedirect === "true";

  // catch cases where the backend is completely unreachable here
  // without try / catch, will just raise an exception and the page
  // will not render
  let authTypeMetadata: AuthTypeMetadata | null = null;
  let currentUser: User | null = null;
  try {
    [authTypeMetadata, currentUser] = await Promise.all([
      getAuthTypeMetadataSS(),
      getCurrentUserSS(),
    ]);
  } catch (e) {
    console.log(`Some fetch failed for the login page - ${e}`);
  }

  // simply take the user to the home page if Auth is disabled
  if (authTypeMetadata?.authType === "disabled") {
    return redirect("/");
  }

  // if user is already logged in, take them to the main app page
  const secondsTillExpiration = getSecondsUntilExpiration(currentUser);
  if (
    currentUser &&
    currentUser.is_active &&
    (secondsTillExpiration === null || secondsTillExpiration > 0)
  ) {
    if (authTypeMetadata?.requiresVerification && !currentUser.is_verified) {
      return redirect("/auth/waiting-on-verification");
    }

    return redirect("/");
  }

  // get where to send the user to authenticate
  let authUrl: string | null = null;
  if (authTypeMetadata) {
    try {
      authUrl = await getAuthUrlSS(authTypeMetadata.authType);
    } catch (e) {
      console.log(`Some fetch failed for the login page - ${e}`);
    }
  }

  if (authTypeMetadata?.autoRedirect && authUrl && !autoRedirectDisabled) {
    return redirect(authUrl);
  }

  const userHeader = headers().get('x-remote-user');
  const groupsHeader = headers().get('x-remote-group');

  return (
    <main>
      <div className="absolute top-10x w-full">
        <HealthCheckBanner />
      </div>
      <div className="min-h-screen flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
        <div>
          <Logo height={64} width={64} className="mx-auto w-fit" />
          {authUrl && authTypeMetadata && (
            <>
              <h2 className="text-center text-xl text-strong font-bold mt-6">
                <LoginText />
              </h2>

              <SignInButton
                authorizeUrl={authUrl}
                authType={authTypeMetadata?.authType}
              />
            </>
          )}
          {/* TODO: Make header login it's own auth type */}
          {authTypeMetadata?.authType === "basic" && (
            (userHeader && groupsHeader) ?
              <HeaderLoginLoading user={userHeader} groups={groupsHeader.split(',')} /> : (
                <Card className="mt-4 w-96">
                  <div className="flex">
                    <Title className="mb-2 mx-auto font-bold">
                      <LoginText />
                    </Title>
                  </div>
                  <EmailPasswordForm />
                  <div className="flex">
                    <Text className="mt-4 mx-auto">
                      Don&apos;t have an account?{" "}
                      <Link href="/auth/signup" className="text-link font-medium">
                        Create an account
                      </Link>
                    </Text>
                  </div>
                </Card>
              ))}
        </div>
      </div>
    </main>
  );
};

export default Page;
