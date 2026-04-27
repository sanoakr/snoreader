import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '../api/client';

export function useFeeds() {
  return useQuery({
    queryKey: ['feeds'],
    queryFn: api.getFeeds,
    refetchInterval: 60_000,
  });
}

export function useCreateFeed() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (url: string) => api.createFeed(url),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feeds'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
    },
  });
}

export function useDeleteFeed() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteFeed(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feeds'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
    },
  });
}

export function useRefreshFeed() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.refreshFeed(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feeds'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
    },
  });
}

export function useImportOpml() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.importOpml(file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feeds'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
    },
  });
}

export function useImportArticles() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.importArticles(file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feeds'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
    },
  });
}
