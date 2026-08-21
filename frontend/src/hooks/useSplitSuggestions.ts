import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '../api/client';

export function useSplitSuggestions() {
  return useQuery({
    queryKey: ['split-suggestions'],
    queryFn: api.getSplitSuggestions,
    staleTime: 60_000,
  });
}

// 適用は辞書と記事分類を両方変えるので、ジャンル系と記事のキャッシュを全部捨てる
function useInvalidateAfterApply() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ['split-suggestions'] });
    qc.invalidateQueries({ queryKey: ['genres'] });
    qc.invalidateQueries({ queryKey: ['genre-counts'] });
    qc.invalidateQueries({ queryKey: ['articles'] });
  };
}

export function useApplySplitSuggestion() {
  const invalidate = useInvalidateAfterApply();
  return useMutation({
    mutationFn: ({ id, labels }: { id: number; labels: Record<string, string> }) =>
      api.applySplitSuggestion(id, labels),
    onSuccess: invalidate,
  });
}

export function useDismissSplitSuggestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.dismissSplitSuggestion(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['split-suggestions'] }),
  });
}

export function useRefreshSplitSuggestions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.refreshSplitSuggestions,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['split-suggestions'] }),
  });
}
