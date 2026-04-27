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
    mutationFn: ({ articleId, name, name_ja }: { articleId: number; name: string; name_ja?: string | null }) =>
      api.addTagToArticle(articleId, name, name_ja),
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

export function useRenameTag() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) => api.renameTag(id, name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
      qc.invalidateQueries({ queryKey: ['article'] });
    },
  });
}

export function useBulkDeleteTags() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (tag_ids: number[]) => api.bulkDeleteTags(tag_ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
      qc.invalidateQueries({ queryKey: ['article'] });
    },
  });
}

export function useAiTagSaved() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.aiTagSaved,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
      qc.invalidateQueries({ queryKey: ['article'] });
    },
  });
}

export function useFillTagTranslations() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.fillTagTranslations,
    onSuccess: () => {
      // バックグラウンドで翻訳中なので2秒後にキャッシュ更新
      setTimeout(() => qc.invalidateQueries({ queryKey: ['tags'] }), 2000);
    },
  });
}
