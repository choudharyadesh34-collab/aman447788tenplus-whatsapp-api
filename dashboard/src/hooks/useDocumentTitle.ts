import { useEffect } from 'react';

/**
 * Custom hook to set document title dynamically.
 * Automatically appends the TenPlus product suffix.
 */
export function useDocumentTitle(title: string) {
  useEffect(() => {
    const previousTitle = document.title;
    document.title = `${title} | TenPlus WhatsApp API`;

    return () => {
      document.title = previousTitle;
    };
  }, [title]);
}
