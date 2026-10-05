import { lazy, Suspense } from 'react';
import TermsGate from './TermsGate';
import AdminGate from './AdminGate';
import ErrorBoundary from './ErrorBoundary';
import PageLoading from './PageLoading';

// The console only downloads after the terms are accepted and the admin has signed in.
const Layout = lazy(() => import('./Layout'));

export default function AdminRoot() {
  return (
    // Terms come before the password prompt: they must be read before signing in, not after.
    <TermsGate>
      <AdminGate>
        <ErrorBoundary>
          <Suspense fallback={<PageLoading fullScreen />}>
            <Layout />
          </Suspense>
        </ErrorBoundary>
      </AdminGate>
    </TermsGate>
  );
}
