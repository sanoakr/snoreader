import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '../api/client';

export function useTags() {
  return useQuery({
    queryKey: ['tags'],
    queryFn: api.getTags,
  });
}

export function useAddTag() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ articleId, name }: { articleId: number; name: string }) =>
      api.addTagToArticle(articleId, name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['article'] });
      qc.invalidateQueries({ queryKey: ['tags'] });
    },
  });
}

export function useRemoveTag() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ articleId, tagId }: { articleId: number; tagId: number }) =>
      api.removeTagFromArticle(articleId, tagId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['article'] });
      qc.invalidateQueries({ queryKey: ['tags'] });
    },
  });
}
